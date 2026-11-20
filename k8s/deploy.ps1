<#
.SYNOPSIS
  One-shot deploy for runner-health-agent onto the local Docker-Desktop
  Kubernetes cluster, working around its Registry Access Management policy
  (which blocks pulling from ad-hoc/custom registries) by importing the
  locally-built image directly into the node's containerd store.

.DESCRIPTION
  1. Builds the Docker image locally (tag: runner-health-agent:v1).
  2. Spins up a short-lived privileged helper pod with host filesystem
     access, imports the image into the node's containerd (k8s.io
     namespace), then removes the helper pod again.
  3. Applies every manifest in k8s/ (namespace, configmap, pvc, deployment,
     service, and secret.yaml). Create configmap.yaml and secret.yaml from
     their sample templates first; see README.md.
  4. Restarts the deployment so it picks up the freshly imported image and
     any changed ConfigMap/Secret values, then waits for the rollout.

.NOTES
  Requires: docker, kubectl, current kube-context = docker-desktop.
  Not meant for real clusters (e.g. EKS) - there you'd push to an actual
  registry (ECR/Artifactory/etc.) instead of this local-import workaround.
#>

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptDir
$imageTag = "runner-health-agent:v1"
$namespace = "runner-health-agent"
$deployment = "runner-health-agent"
$nodeName = "desktop-control-plane"
$helperPod = "node-loader"

function Assert-Context {
    $ctx = kubectl config current-context
    if ($ctx -ne "docker-desktop") {
        throw "Current kubectl context is '$ctx', expected 'docker-desktop'. Run 'kubectl config use-context docker-desktop' first, or edit this script if you're intentionally targeting another cluster."
    }
}

function Assert-PrivateConfigFiles {
    foreach ($name in @("configmap", "secret")) {
        $configPath = Join-Path $scriptDir "$name.yaml"
        if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
            throw "k8s/$name.yaml not found. Copy k8s/$name.sample.yaml to k8s/$name.yaml and configure your private values before running this script. Never commit the private file."
        }
    }
}

Write-Host "==> Checking prerequisites" -ForegroundColor Cyan
Assert-Context
Assert-PrivateConfigFiles

Write-Host "==> Building image $imageTag" -ForegroundColor Cyan
Push-Location $repoRoot
try {
    docker build -t $imageTag . | Select-Object -Last 5
} finally {
    Pop-Location
}

Write-Host "==> Importing image into the node's containerd store" -ForegroundColor Cyan
$tarPath = Join-Path $env:TEMP "runner-health-agent-deploy.tar"
docker save -o $tarPath $imageTag

kubectl delete pod $helperPod --ignore-not-found | Out-Null
$overrides = '{\"spec\":{\"nodeName\":\"' + $nodeName + '\",\"hostPID\":true,\"containers\":[{\"name\":\"' + $helperPod + '\",\"image\":\"alpine\",\"command\":[\"sleep\",\"3600\"],\"securityContext\":{\"privileged\":true},\"volumeMounts\":[{\"name\":\"host\",\"mountPath\":\"/host\"}]}],\"volumes\":[{\"name\":\"host\",\"hostPath\":{\"path\":\"/\"}}]}}'
kubectl run $helperPod --image=alpine --restart=Never --overrides=$overrides | Out-Null
kubectl wait --for=condition=Ready "pod/$helperPod" --timeout=30s | Out-Null

Push-Location $env:TEMP
try {
    kubectl cp (Split-Path -Leaf $tarPath) "default/${helperPod}:/host/tmp/image.tar"
} finally {
    Pop-Location
}
kubectl exec $helperPod -- chroot /host ctr -n k8s.io images import /tmp/image.tar

Write-Host "==> Removing helper pod" -ForegroundColor Cyan
kubectl delete pod $helperPod --ignore-not-found | Out-Null
Remove-Item $tarPath -ErrorAction SilentlyContinue

Write-Host "==> Applying all manifests (including secret.yaml)" -ForegroundColor Cyan
kubectl apply -k $scriptDir

Write-Host "==> Restarting deployment to pick up the new image/config" -ForegroundColor Cyan
kubectl -n $namespace rollout restart "deployment/$deployment"
kubectl -n $namespace rollout status "deployment/$deployment" --timeout=90s

Write-Host "==> Done. Current state:" -ForegroundColor Green
kubectl -n $namespace get pods,pvc,svc
