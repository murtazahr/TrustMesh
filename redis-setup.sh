#!/bin/bash

# Function to generate SSL certificates
generate_ssl_certs() {
    mkdir -p ssl
    # Generate CA key and certificate
    openssl genrsa -out ssl/ca.key 4096
    openssl req -x509 -new -nodes -key ssl/ca.key -sha256 -days 1024 -out ssl/ca.crt -subj "/CN=Redis-Cluster-CA"

    # Generate server key and certificate signed by the CA
    openssl genrsa -out ssl/redis.key 2048
    openssl req -new -key ssl/redis.key -out ssl/redis.csr -subj "/CN=redis-cluster"
    openssl x509 -req -in ssl/redis.csr -CA ssl/ca.crt -CAkey ssl/ca.key -CAcreateserial -out ssl/redis.crt -days 365 -sha256

    # Create Kubernetes secret with all certificates
    kubectl create secret generic redis-certificates --from-file=ssl
}

generate_password() {
    openssl rand -base64 32 | tr -d "=+/" | cut -c1-32
}

# Function to create Redis password secret
create_redis_password_secret() {
    local redis_password=$1
    kubectl create secret generic redis-password --from-literal=password=$redis_password
}

# Function to generate compute node names based on count
generate_compute_nodes() {
    local num_nodes=$1
    for i in $(seq 1 $num_nodes); do
        echo "compute-node-$i"
    done
}

# Function to randomly select unique compute nodes
select_unique_compute_nodes() {
    local num_redis_nodes=$1
    local total_compute_nodes=$2

    if [ $total_compute_nodes -lt $num_redis_nodes ]; then
        echo "Error: Not enough compute nodes available. Found $total_compute_nodes, need $num_redis_nodes." >&2
        exit 1
    fi

    local compute_nodes=($(generate_compute_nodes $total_compute_nodes))
    shuf -e "${compute_nodes[@]}" | head -n $num_redis_nodes
}

generate_redis_cluster_yaml() {
    local num_redis_nodes=$1
    local redis_password=$2
    shift 2
    local selected_nodes=("$@")

    cat << EOF
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: redis-cluster-config
data:
  redis.conf: |
    port 0
    tls-port 6379
    tls-cert-file /ssl/redis.crt
    tls-key-file /ssl/redis.key
    tls-ca-cert-file /ssl/ca.crt
    tls-auth-clients no
    tls-replication yes
    tls-cluster yes
    requirepass ${redis_password}
    masterauth ${redis_password}
    protected-mode yes
    cluster-enabled yes
    cluster-config-file nodes.conf
    cluster-node-timeout 5000
    appendonly yes
    appendfsync everysec
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: redis-cluster
spec:
  serviceName: redis-cluster
  replicas: $num_redis_nodes
  selector:
    matchLabels:
      app: redis-cluster
  template:
    metadata:
      labels:
        app: redis-cluster
    spec:
      affinity:
        podAntiAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
          - labelSelector:
              matchExpressions:
              - key: app
                operator: In
                values:
                - redis-cluster
            topologyKey: "kubernetes.io/hostname"
        nodeAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            nodeSelectorTerms:
            - matchExpressions:
              - key: kubernetes.io/hostname
                operator: In
                values:
EOF
    for node in "${selected_nodes[@]}"; do
        echo "                - $node"
    done
    cat << EOF
      containers:
      - name: redis
        image: redis:6.2
        ports:
        - containerPort: 6379
          name: tls
        command: ["redis-server", "/conf/redis.conf"]
        resources:
          requests:
            cpu: 500m
            memory: 750Mi
          limits:
            cpu: 1
            memory: 1Gi
        volumeMounts:
        - name: conf
          mountPath: /conf
          readOnly: false
        - name: data
          mountPath: /data
        - name: ssl
          mountPath: /ssl
          readOnly: true
      volumes:
      - name: conf
        configMap:
          name: redis-cluster-config
          items:
          - key: redis.conf
            path: redis.conf
      - name: ssl
        secret:
          secretName: redis-certificates
  volumeClaimTemplates:
  - metadata:
      name: data
    spec:
      accessModes: ["ReadWriteOnce"]
      resources:
        requests:
          storage: 2Gi
---
apiVersion: v1
kind: Service
metadata:
  name: redis-cluster
spec:
  selector:
    app: redis-cluster
  clusterIP: None
  ports:
  - port: 6379
    targetPort: 6379
    name: tls
EOF
}

# Function to wait for all Redis Cluster pods to be running
wait_for_redis_pods() {
    echo "Waiting for all Redis Cluster pods to be running..."
    while true; do
        running_pods=$(kubectl get pods -l app=redis-cluster -o jsonpath='{range .items[*]}{.status.phase}{"\n"}{end}' | grep -c "Running")
        total_pods=$(kubectl get pods -l app=redis-cluster -o jsonpath='{range .items[*]}{.status.phase}{"\n"}{end}' | wc -l)

        if [ "$running_pods" -eq "$total_pods" ]; then
            echo "All $total_pods Redis Cluster pods are now running."
            break
        else
            echo "$running_pods out of $total_pods pods are running. Waiting..."
            sleep 10
        fi
    done
}

# Function to check cluster status with retries
check_cluster_status() {
    local max_retries=5
    local retry_interval=10
    local retry_count=0

    while [ $retry_count -lt $max_retries ]; do
        echo "Checking cluster status (attempt $((retry_count+1))/$max_retries)..."
        status=$(kubectl exec -it redis-cluster-0 -- redis-cli --tls --cert /ssl/redis.crt --key /ssl/redis.key --cacert /ssl/ca.crt -a $redis_password cluster info | grep cluster_state | cut -d: -f2 | tr -d '[:space:]')

        if [ "$status" = "ok" ]; then
            echo "Cluster is now in OK state."
            return 0
        else
            echo "Cluster state is still: $status. Waiting $retry_interval seconds before next check."
            sleep $retry_interval
            retry_count=$((retry_count+1))
        fi
    done

    echo "Cluster failed to reach OK state after $max_retries attempts."
    return 1
}

# Check if number of compute nodes argument is provided
if [ $# -ne 1 ]; then
    echo "Usage: $0 <number_of_compute_nodes>"
    exit 1
fi

total_compute_nodes=$1

# Validate input is a positive integer
if ! [[ "$total_compute_nodes" =~ ^[1-9][0-9]*$ ]]; then
    echo "Error: Please provide a positive integer for the number of compute nodes."
    exit 1
fi

# Main script execution
generate_ssl_certs

# Generate password
redis_password=$(generate_password)

# Create Redis password secret
create_redis_password_secret "$redis_password"

# Calculate the number of Redis nodes (minimum 4, maximum 10)
num_redis_nodes=$(( total_compute_nodes > 10 ? 10 : total_compute_nodes ))
echo "Number of Redis nodes to be created: $num_redis_nodes"

# Randomly select from all available compute nodes
readarray -t selected_nodes < <(select_unique_compute_nodes $num_redis_nodes $total_compute_nodes)
echo "Selected compute nodes: ${selected_nodes[*]}"

# Generate and apply the Redis Cluster YAML
generate_redis_cluster_yaml $num_redis_nodes "$redis_password" "${selected_nodes[@]}" > kubernetes-manifests/generated/redis-cluster.yaml
kubectl apply -f kubernetes-manifests/generated/redis-cluster.yaml

# Wait for all pods to be in the running state
wait_for_redis_pods

# Get the list of Redis node IPs
node_ips=$(kubectl get pods -l app=redis-cluster -o jsonpath='{range.items[*]}{.status.podIP}{" "}{end}')

# Create the Redis Cluster
echo "Creating Redis Cluster with $num_redis_nodes nodes..."
kubectl exec -it redis-cluster-0 -- redis-cli --cluster create \
    $(echo $node_ips | sed -e 's/\([0-9.]*\)/\1:6379/g') \
    --cluster-replicas $(( (num_redis_nodes - 3) / 3 )) \
    --tls --cert /ssl/redis.crt --key /ssl/redis.key --cacert /ssl/ca.crt -a $redis_password

echo "Waiting for cluster to stabilize..."
sleep 10  # Give the cluster some time to stabilize

# Check cluster status with retries
if check_cluster_status; then
    echo "Redis Cluster is now fully operational."
else
    echo "Warning: Redis Cluster may not be fully operational. Please check manually."
fi

rm -r ssl/