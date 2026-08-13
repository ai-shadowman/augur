# Augur OpenShift Cluster Setup on demo.redhat.com

Environment setup for the Augur harness: provision an OpenShift cluster with a GPU worker on demo.redhat.com, install the AI Accelerator (RHOAI), deploy MinIO and the models.

## Contents

1. [Order the OpenShift Cluster](#1-order-the-openshift-cluster)
2. [AWS GPU Provisioning](#2-aws-gpu-provisioning)
3. [AI Accelerator](#3-ai-accelerator)
4. [Deploy MinIO](#4-deploy-minio)
5. [Deploy the Embedding Model](#5-deploy-the-embedding-model-e5-mistral-7b-instruct)

---

## 1. Order the OpenShift Cluster

Order an OCP4 cluster from the [demo.redhat.com catalog](https://catalog.demo.redhat.com/catalog/babylon-catalog-prod?item=babylon-catalog-prod/published.ocp4-cluster.prod&utm_source=webapp&utm_medium=share-link).

Select:

| Option | Value |
| --- | --- |
| Opp ID | << redacted >> |
| Cloud provider | **aws** |
| OpenShift Version | **latest** |
| Cluster size | **multinode** |
| OpenShift Worker count | **2** |
| OpenShift Worker instance type | **largest** |
| Enable OPEN environment | ✅ |

> **Note:** Limit for GPU vGPUs is 64 by default.

Once provisioned:

1. Login to OCP.
2. Snag the command line login (`oc login ...`) from the console.

## 2. AWS GPU Provisioning

Create a working directory:

```bash
mkdir ai-test && cd ai-test/
```

### Choose your fighter

Pick a GPU instance type for the new worker:

| Instance type | GPU | Approx. cost |
| --- | --- | --- |
| `p5.4xlarge` | H100 | $6.88/hr |
| `p4d.24xlarge` | A100 | $21.958/hr |
| `g6e.2xlarge` | L40 | $2.242/hr |
| `g4dn.4xlarge` | T4 | $1.204/hr |
| `g4dn.12xlarge` | 4x T4 | — |

This guide uses `g6e.8xlarge`:

```bash
AWS_GPU_INSTANCE_TYPE=g6e.8xlarge
```

### Inspect the cluster

```bash
oc get nodes

oc get mcp

oc get machinesets -n openshift-machine-api
```

### Create the GPU MachineSet

Clone an existing worker MachineSet, retarget it at the GPU instance type, and create it:

```bash
BASE_MACHINE_SET=$(oc get machinesets -n openshift-machine-api | grep "2a " | awk '{print $1}')

echo $BASE_MACHINE_SET

oc get machineset/$BASE_MACHINE_SET -n openshift-machine-api -o yaml | yq 'del(.status, .metadata.uid, .metadata.creationTimestamp, .metadata.resourceVersion, .metadata.generation, .metadata.managedFields, .metadata.selfLink, .metadata.ownerReferences, .metadata.annotations)' > GPU-worker-machineset-$AWS_GPU_INSTANCE_TYPE.yaml

sed -i "s/instanceType:.*/instanceType: $AWS_GPU_INSTANCE_TYPE/" GPU-worker-machineset-$AWS_GPU_INSTANCE_TYPE.yaml

sed -i "s/volumeSize:.*/volumeSize: 1000/" GPU-worker-machineset-$AWS_GPU_INSTANCE_TYPE.yaml

sed -i -E "s/ocp-([a-zA-Z0-9]{5})-worker-us-east-2a/ocp-\1-gpu-$AWS_GPU_INSTANCE_TYPE-worker-us-east-2a/g" GPU-worker-machineset-$AWS_GPU_INSTANCE_TYPE.yaml

# Going with 1 replica to host the embedding llm
sed -i "s/replicas:.*/replicas: 1/" GPU-worker-machineset-$AWS_GPU_INSTANCE_TYPE.yaml

oc create -f GPU-worker-machineset-$AWS_GPU_INSTANCE_TYPE.yaml
```

> **Note:** Probably need to wait like 15-20 minutes here. Can always go in AWS console to check.

### Verify the GPU node

```bash
oc get nodes

GPU_MACHINE_SET=$(oc get machinesets -n openshift-machine-api | grep "gpu" | awk '{print $1}')
echo $GPU_MACHINE_SET

oc get machine -n openshift-machine-api  -l machine.openshift.io/cluster-api-machineset=$GPU_MACHINE_SET -o jsonpath='{range .items[*]}{.status.nodeRef.name}{"\n"}{end}'

# Set GPU_NODE_NAME to one of the node names printed by the previous command
oc debug node/$GPU_NODE_NAME -- sh -c "chroot /host; lspci | grep -i nvidia"
```

## 3. AI Accelerator

Bootstrap Red Hat OpenShift AI (RHOAI) and supporting operators via the [ai-accelerator](https://github.com/redhat-ai-services/ai-accelerator) project:

```bash
git clone https://github.com/redhat-ai-services/ai-accelerator

cd ai-accelerator/

./bootstrap.sh

# When prompted for an overlay, choose: 6) rhoai-stable-3.x-aws-gpu

cd ..
```

## 4. Deploy MinIO

Deploys MinIO into the `openshiftai-minio` namespace: a PVC, root-credentials secret (`minio` / `minio123`), deployment, service, and edge-terminated routes for the API (port 9000) and console UI (port 9090).

```bash
cat << 'EOF' > minio.yaml
---
apiVersion: v1
kind: Namespace
metadata:
  labels:
    openshift.io/cluster-monitoring: "true"
  name: openshiftai-minio
---
kind: PersistentVolumeClaim
apiVersion: v1
metadata:
  name: minio-pvc
  namespace: openshiftai-minio
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 20Gi
  volumeMode: Filesystem
---
kind: Secret
apiVersion: v1
metadata:
  name: minio-secret
stringData:
  minio_root_user: minio
  minio_root_password: minio123
---
kind: Deployment
apiVersion: apps/v1
metadata:
  name: minio
  namespace: openshiftai-minio
spec:
  replicas: 1
  selector:
    matchLabels:
      app: minio
  template:
    metadata:
      labels:
        app: minio
    spec:
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: minio-pvc
      containers:
        - name: minio
          image: quay.io/minio/minio@sha256:14cea493d9a34af32f524e538b8346cf79f3321eff8e708c1e2960462bd8936e
          imagePullPolicy: IfNotPresent
          args:
            - server
            - /data
            - --console-address
            - :9090
          ports:
            - containerPort: 9000
              protocol: TCP
            - containerPort: 9090
              protocol: TCP
          env:
            - name: MINIO_ROOT_USER
              valueFrom:
                secretKeyRef:
                  name: minio-secret
                  key: minio_root_user
            - name: MINIO_ROOT_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: minio-secret
                  key: minio_root_password
          resources:
            limits:
              cpu: 250m
              memory: 1Gi
            requests:
              cpu: 20m
              memory: 100Mi
          readinessProbe:
            tcpSocket:
              port: 9000
            initialDelaySeconds: 5
            timeoutSeconds: 1
            periodSeconds: 5
            successThreshold: 1
            failureThreshold: 3
          livenessProbe:
            tcpSocket:
              port: 9000
            initialDelaySeconds: 30
            timeoutSeconds: 1
            periodSeconds: 5
            successThreshold: 1
            failureThreshold: 3
          volumeMounts:
            - name: data
              mountPath: /data
              subPath: minio
          terminationMessagePath: /dev/termination-log
          terminationMessagePolicy: File
      restartPolicy: Always
      terminationGracePeriodSeconds: 30
      dnsPolicy: ClusterFirst
      securityContext: {}
      schedulerName: default-scheduler
  strategy:
    type: Recreate
  revisionHistoryLimit: 10
  progressDeadlineSeconds: 600
---
kind: Service
apiVersion: v1
metadata:
  name: minio-service
  namespace: openshiftai-minio
spec:
  ipFamilies:
    - IPv4
  ports:
    - name: api
      protocol: TCP
      port: 9000
      targetPort: 9000
    - name: ui
      protocol: TCP
      port: 9090
      targetPort: 9090
  internalTrafficPolicy: Cluster
  type: ClusterIP
  ipFamilyPolicy: SingleStack
  sessionAffinity: None
  selector:
    app: minio
---
kind: Route
apiVersion: route.openshift.io/v1
metadata:
  name: minio-api
  namespace: openshiftai-minio
spec:
  to:
    kind: Service
    name: minio-service
    weight: 100
  port:
    targetPort: api
  wildcardPolicy: None
  tls:
    termination: edge
    insecureEdgeTerminationPolicy: Redirect
---
kind: Route
apiVersion: route.openshift.io/v1
metadata:
  name: minio-ui
  namespace: openshiftai-minio
spec:
  to:
    kind: Service
    name: minio-service
    weight: 100
  port:
    targetPort: ui
  wildcardPolicy: None
  tls:
    termination: edge
    insecureEdgeTerminationPolicy: Redirect
EOF
oc create -f minio.yaml -n openshiftai-minio
```

## 5. Deploy the Embedding Model (e5-mistral-7b-instruct)

Create the project:

```bash
oc new-project model-e5-mistral-7b-instruct
```

Then deploy the model from the RHOAI dashboard (GUI) with these settings:

| Setting | Value |
| --- | --- |
| Model location (OCI) | `oci://quay.io/ai-shadowman/e5-mistral-7b-instruct:latest` |
| Hardware profile | Nvidia-gpu |
| Memory (request / limit) | 16 / 16 |
| Deployment resource | vLLM NVIDIA GPU ServingRuntime for KServe |
| Custom runtime argument | `--runner=pooling` |
| Require token authentication | ✅ — Service account name: `default-token` |
| Deployment strategy | Recreate |
