// Backend pipeline: lint -> build -> push -> deploy (backend only).
// The frontend has its own Jenkinsfile in tripsync-frontend; the Helm chart
// lives in tripsync-devops and is checked out next to this repo.
pipeline {
    // Run every stage on the agent VM, never on the controller.
    agent { label 'k3s-agent' }

    options {
        // We check out into subfolders ourselves (see 'Checkout'), so turn off
        // Jenkins' automatic checkout into the workspace root. Otherwise the
        // devops repo would end up nested inside the backend repo and inside
        // the Docker build context.
        skipDefaultCheckout()
        // Two backend builds running helm upgrade at once would collide.
        disableConcurrentBuilds()
        buildDiscarder(logRotator(numToKeepStr: '20'))
    }

    // Jenkins is only reachable over Tailscale, so GitHub webhooks can't reach
    // it. Instead Jenkins asks GitHub every ~5 minutes "anything new?".
    // 'H' spreads jobs across the interval so they don't all poll at once.
    // Note: the trigger is registered only after the job has run once.
    triggers { pollSCM('H/5 * * * *') }

    // "Build with Parameters" shows a dropdown. The first choice is the
    // default, and SCM-polling builds always use the default, so automatic
    // builds keep deploying to the homelab. Pick 'eks' manually for AWS.
    // Note: Jenkins only learns about this block after one build has run it.
    parameters {
        choice(name: 'TARGET', choices: ['k3s', 'eks'],
               description: 'Cluster to deploy to: homelab k3s or AWS EKS')
    }

    environment {
        REGISTRY_USER = 'swagarot'
        IMAGE_NAME    = 'swagarot/tripsync-backend'
    }

    stages {
        stage('Checkout') {
            steps {
                // 'scm' = the repo/branch/credentials configured in the job
                // itself (this repo). Polling watches this checkout.
                dir('backend') {
                    checkout scm
                }
                // The Helm chart (and values-k3s.yaml, which Deploy commits to).
                // Cloned with plain git on purpose: Jenkins polls every repo
                // checked out by a git/checkout step, and even with poll: false
                // tripsync-devops commits started both jobs (proven 2026-10-31
                // from Docker Hub push times). With GitOps every "Deploy" commit
                // then rebuilt both apps. Polling can't see a shell clone, so
                // only commits to this repo (backend) start a backend build.
                withCredentials([gitUsernamePassword(
                        credentialsId: '6daea266-02fa-4ffd-ad67-4ab97eec3938',
                        gitToolName: 'Default')]) {
                    sh '''
                        rm -rf devops
                        git clone -q --branch main                           https://github.com/Swagarot/tripsync-devops.git devops
                    '''
                }
            }
        }

        stage('Lint') {
            parallel {
                stage('Helm chart') {
                    steps {
                        // Lint as EKS (defaults) and as k3s (what Argo CD renders).
                        sh '''
                            helm lint devops/k8s-helm/tripsync
                            helm lint devops/k8s-helm/tripsync -f devops/k8s-helm/tripsync/values-k3s.yaml
                        '''
                    }
                }
                stage('flake8') {
                    steps {
                        // Throwaway container, run as the jenkins user so no
                        // root-owned files are left in the workspace.
                        sh '''
                            docker run --rm -u $(id -u):$(id -g) -e HOME=/tmp \
                              -v "$PWD/backend:/src" -w /src python:3.11-slim \
                              sh -c "pip install --user -q flake8 && python -m flake8 . --max-line-length=120"
                        '''
                    }
                }
            }
        }

        stage('Unit tests') {
            environment {
                // One private Docker network + database per build, so nothing
                // is shared with other builds and cleanup is just two removals.
                TEST_NET = "tripsync-pytest-${env.BUILD_NUMBER}"
            }
            steps {
                // Tests run against real Postgres (same major version as the
                // cluster), not SQLite: the schema is created by the actual
                // migrations (`flask db upgrade`), so a broken migration fails
                // here instead of in the init container during a deploy.
                // The DB password is a throwaway for a container that lives a
                // minute and is reachable only on its private network.
                sh '''
                    docker network create "$TEST_NET"
                    docker run -d --name "$TEST_NET-db" --network "$TEST_NET" \
                      -e POSTGRES_USER=test -e POSTGRES_PASSWORD=test -e POSTGRES_DB=test \
                      postgres:18-alpine
                    for i in $(seq 1 30); do
                      docker exec "$TEST_NET-db" pg_isready -U test -q && break
                      sleep 2
                    done
                    docker run --rm --network "$TEST_NET" -u $(id -u):$(id -g) -e HOME=/tmp \
                      -e POSTGRES_HOST="$TEST_NET-db" -e POSTGRES_USER=test \
                      -e POSTGRES_PASSWORD=test -e POSTGRES_DB=test -e JWT_SECRET_KEY=test \
                      -e TEST_DATABASE_URL="postgresql+psycopg://test:test@$TEST_NET-db:5432/test" \
                      -v "$PWD/backend:/src" -w /src python:3.11-slim \
                      sh -c "pip install --user -q -r requirements-dev.txt \
                             && python -m flask db upgrade \
                             && python -m pytest -q --junitxml=test-results.xml"
                '''
            }
            post {
                always {
                    // Test results show up on the build page (JUnit plugin).
                    junit allowEmptyResults: true, testResults: 'backend/test-results.xml'
                    sh 'docker rm -f "$TEST_NET-db" || true; docker network rm "$TEST_NET" || true'
                }
            }
        }

        stage('Set image tag') {
            steps {
                script {
                    // Short SHA of the backend repo, so every build gets a
                    // unique tag and Kubernetes actually rolls out the change.
                    env.IMAGE_TAG = sh(
                        script: 'git -C backend rev-parse --short HEAD',
                        returnStdout: true
                    ).trim()
                    echo "Image tag: ${env.IMAGE_TAG}"
                }
            }
        }

        stage('Build image') {
            steps {
                sh 'docker build -t $IMAGE_NAME:$IMAGE_TAG backend'
            }
        }

        stage('Push image') {
            steps {
                // withCredentials exposes the stored secret as env vars only
                // inside this block, and masks them in the console log.
                // Triple single quotes: the shell expands $DH_TOKEN, not
                // Groovy, so the secret never gets interpolated into the script.
                // 'latest' is pushed too: on a fresh cluster the first install
                // only sets our tag, so the other service falls back to
                // values.yaml's 'latest', which must exist and be recent.
                // Deploys still use the SHA tag.
                withCredentials([usernamePassword(
                    credentialsId: 'dockerhub-creds',
                    usernameVariable: 'DH_USER',
                    passwordVariable: 'DH_TOKEN')]) {
                    sh '''
                        echo "$DH_TOKEN" | docker login -u "$DH_USER" --password-stdin
                        docker push $IMAGE_NAME:$IMAGE_TAG
                        docker tag $IMAGE_NAME:$IMAGE_TAG $IMAGE_NAME:latest
                        docker push $IMAGE_NAME:latest
                    '''
                }
            }
        }

        stage('Deploy') {
            steps {
                // Lockable Resources plugin. The frontend job takes the same
                // lock, so only one deploy runs at a time: on EKS one helm
                // upgrade of the shared 'tripsync' release (the other job
                // waits instead of failing with "another operation is in
                // progress"), on k3s one push to tripsync-devops.
                lock('tripsync-release') {
                    // EKS: --reset-then-reuse-values: start from values.yaml,
                    // re-apply the --set values of the previous release (e.g.
                    // the frontend tag), then apply ours. Without it the
                    // frontend tag would be reset to 'latest'. Needs Helm 3.14+.
                    // --install: if the release doesn't exist yet (fresh
                    // cluster), install it instead of failing.
                    script {
                        if (params.TARGET == 'eks') {
                            // Two Secret-text credentials, exposed as the env
                            // vars the AWS CLI reads, only inside this block.
                            withCredentials([
                                string(credentialsId: 'aws-access-key-id',
                                       variable: 'AWS_ACCESS_KEY_ID'),
                                string(credentialsId: 'aws-secret-access-key',
                                       variable: 'AWS_SECRET_ACCESS_KEY')]) {
                                // Separate kubeconfig in the workspace, so the
                                // jenkins user's ~/.kube/config (k3s) is never
                                // touched. It holds no secret: kubectl/helm run
                                // 'aws eks get-token' with the keys above.
                                // On EKS the release is stored in 'tripsync'.
                                // Postgres's disk is a gp3 EBS volume there
                                // (StorageClass made by terraform/aws), not Longhorn.
                                sh '''
                                    export AWS_DEFAULT_REGION=eu-central-1
                                    export KUBECONFIG="$WORKSPACE/kubeconfig-eks"
                                    aws eks update-kubeconfig --name tripsync-cluster \
                                      --kubeconfig "$WORKSPACE/kubeconfig-eks"
                                    helm upgrade --install tripsync devops/k8s-helm/tripsync -n tripsync \
                                      --reset-then-reuse-values \
                                      --set-string backend.image.tag=$IMAGE_TAG \
                                      --set postgres.storageClassName=gp3 \
                                      --atomic --timeout 3m
                                    kubectl get pods -n tripsync
                                '''
                            }
                        } else {
                            // k3s: GitOps. Jenkins doesn't touch the cluster:
                            // it writes the new tag into values-k3s.yaml in
                            // tripsync-devops and pushes, and Argo CD (running
                            // in k3s) rolls it out within about a minute. The
                            // commit is the deploy record; git revert = rollback.
                            // gitUsernamePassword: the git plugin gives the
                            // GitHub token to git through GIT_ASKPASS, so it is
                            // never in a URL or in the log. The token needs
                            // write access to tripsync-devops.
                            withCredentials([gitUsernamePassword(
                                    credentialsId: '6daea266-02fa-4ffd-ad67-4ab97eec3938',
                                    gitToolName: 'Default')]) {
                                // yq (throwaway container) changes one key and
                                // keeps the file's comments. No change = no
                                // commit, so rebuilding the same SHA is a no-op.
                                // If the other job pushed in between, the push
                                // is refused: start again from the new main.
                                sh '''
                                    cd devops
                                    git config user.name "Jenkins"
                                    git config user.email "jenkins@tripsync.invalid"
                                    for attempt in 1 2 3; do
                                      git fetch -q origin main
                                      git reset -q --hard origin/main
                                      docker run --rm -u $(id -u):$(id -g) -e IMAGE_TAG \
                                        -v "$PWD:/w" -w /w mikefarah/yq:4.54.1 -i \
                                        '.backend.image.tag = strenv(IMAGE_TAG)' \
                                        k8s-helm/tripsync/values-k3s.yaml
                                      if git diff --quiet; then
                                        echo "values-k3s.yaml already has backend $IMAGE_TAG: nothing to deploy"
                                        exit 0
                                      fi
                                      git commit -q -am "Deploy backend $IMAGE_TAG"
                                      if git push -q origin HEAD:main; then
                                        echo "Pushed. Argo CD deploys it; watch it on port 30443."
                                        exit 0
                                      fi
                                      echo "Push refused (the other job pushed first?), retrying"
                                      sleep 5
                                    done
                                    exit 1
                                '''
                            }
                        }
                    }
                }
            }
        }
    }

    post {
        always {
            sh 'docker logout || true'
        }
    }
}
