// ralphd CI on the homelab Jenkins: multibranch job ralphd/ci (see docs/ci.md).
//
// Every branch: Prepare (lane images, ci/jenkins-image.sh) -> the four tiers
// in parallel, each in its own container and checkout (ci/jenkins-lane.sh
// runs ci/jenkins-tier.sh <tier>):
//   Lint     ruff, the E9/F63/F7/F821 selection        15 min, 4 CPUs
//   Fast     -m fast -n 4, python:3.12.3               45 min, CPUs 0-3, 12 GB
//   Docker   -m docker, host docker socket             120 min, 4 CPUs, 12 GB
//   Browser  -m browser, playwright-cli + Chrome       60 min, 4 CPUs, 16 GB
// main also runs nightly (02:23) in the nightly lane, which adds the six
// dev-layout cells (docs/testing.md); the EXTENDED parameter asks for it on
// demand. Reports are Jenkins HTML reports on the build page ("Allure", "CI
// summary"), built in post { always } so a red build still has them.
//
// Telegram: notifyTelegram from the homelab shared library
// (github.com/n-orlov/jenkins-library), last, in post { cleanup }.
@Library('homelab') _

pipeline {
  agent { label 'linux && amd64 && docker' }

  options {
    ansiColor('xterm')
    timestamps()
    buildDiscarder(logRotator(numToKeepStr: '30'))
    disableConcurrentBuilds()
    // One ralphd/ci build at a time across all branches: the lanes and cells
    // size themselves to the whole agent, and two builds at once push the
    // fast tier past its bound (lockable-resources; docs/ci.md).
    lock('ralphd-ci')
    timeout(time: 420, unit: 'MINUTES')   // ceiling; lanes and cells carry their own
    // This job's next build of a branch reads allure-history/ (the pattern
    // matches the copier's absolute name).
    copyArtifactPermission('/ralphd/ci/*')
  }

  parameters {
    booleanParam(name: 'EXTENDED', defaultValue: false,
                 description: 'Nightly lane: also run the six dev-layout cells')
  }

  triggers {
    cron(env.BRANCH_NAME == 'main' ? '23 2 * * *' : '')
  }

  environment {
    // The fast and lint lanes' image, and the root helper ci/jenkins-reap.sh runs.
    PY_IMAGE = 'python:3.12.3@sha256:3966b81808d864099f802080d897cef36c01550472ab3955fdd716d1c665acd6'
  }

  stages {
    stage('Prepare') {
      steps {
        script {
          def nightly = params.EXTENDED || !currentBuild.getBuildCauses('hudson.triggers.TimerTrigger$TimerTriggerCause').isEmpty()
          env.CI_NIGHTLY = nightly ? 'true' : 'false'
          // The docker tier tags its images ralphd:test-<RALPHD_RUN_ID>.
          env.RALPHD_RUN_ID = 'ci-' + env.BUILD_TAG.toLowerCase().replaceAll('[^a-z0-9_.-]', '-')
          if (nightly) {
            currentBuild.description = 'nightly (dev-layout cells)'
          }
        }
        // The workspace persists between builds: hand back what the last
        // one's containers wrote, then start from the checkout alone.
        sh 'ci/jenkins-reap.sh'
        sh 'git clean -ffdxq'
        script {
          env.BROWSER_IMAGE = sh(returnStdout: true, script: 'ci/jenkins-image.sh browser').trim()
          env.DOCKER_TIER_IMAGE = sh(returnStdout: true, script: 'ci/jenkins-image.sh docker-tier').trim()
        }
      }
    }

    stage('Tiers') {
      parallel {
        stage('Lint') {
          steps {
            timeout(time: 15, unit: 'MINUTES') {
              catchError(buildResult: 'FAILURE', stageResult: 'FAILURE') {
                script {
                  env.LINT_RESULT = 'failing'
                  sh 'ci/jenkins-lane.sh lint 15 "$PY_IMAGE" --cpus=4 --cpuset-cpus=4-11 -- ci/jenkins-tier.sh lint'
                  env.LINT_RESULT = 'passing'
                }
              }
            }
          }
        }
        stage('Fast') {
          steps {
            timeout(time: 45, unit: 'MINUTES') {
              catchError(buildResult: 'FAILURE', stageResult: 'FAILURE') {
                // The agent's first four CPUs, with first claim on them: the
                // other lanes run on the rest (agent: 12 CPUs), and several
                // fast tests hold millisecond-scale deadlines that miss when
                // the lanes contend. The old runner scaler gave the same
                // guarantee by never admitting more than 11 CPUs of jobs.
                sh 'ci/jenkins-lane.sh fast 45 "$PY_IMAGE" --cpus=4 --cpuset-cpus=0-3 --cpu-shares=4096 --memory=12g --pids-limit=4096 -- ci/jenkins-tier.sh fast'
              }
            }
          }
        }
        stage('Docker') {
          steps {
            timeout(time: 120, unit: 'MINUTES') {
              catchError(buildResult: 'FAILURE', stageResult: 'FAILURE') {
                sh 'ci/jenkins-lane.sh docker 120 "$DOCKER_TIER_IMAGE" --cpus=4 --cpuset-cpus=4-11 --memory=12g --pids-limit=4096 -v /var/run/docker.sock:/var/run/docker.sock -- ci/jenkins-tier.sh docker'
              }
            }
          }
        }
        stage('Browser') {
          steps {
            timeout(time: 60, unit: 'MINUTES') {
              catchError(buildResult: 'FAILURE', stageResult: 'FAILURE') {
                sh 'ci/jenkins-lane.sh browser 60 "$BROWSER_IMAGE" --cpus=4 --cpuset-cpus=4-11 --memory=16g --pids-limit=4096 --shm-size=1g -- ci/jenkins-tier.sh browser'
              }
            }
          }
        }
      }
    }

    // Six cells (three images x PYTHONUSERBASE exported/unset), 2 CPUs each,
    // three at a time: one wave per mode.
    stage('Dev-layout cells') {
      when { environment name: 'CI_NIGHTLY', value: 'true' }
      steps {
        script {
          for (mode in ['exported', 'unset']) {
            def cells = [:]
            for (image in ['ubuntu:24.04', 'python:3.11-slim', 'python:3.14-slim']) {
              def cellImage = image
              def cellMode = mode
              cells[cellImage + ' ' + cellMode] = {
                timeout(time: 120, unit: 'MINUTES') {
                  catchError(buildResult: 'FAILURE', stageResult: 'FAILURE') {
                    sh "CELL_MINUTES=120 ci/jenkins-devlayout.sh '${cellImage}' ${cellMode}"
                  }
                }
              }
            }
            parallel cells
          }
        }
      }
    }
  }

  post {
    always {
      catchError(buildResult: 'UNSTABLE', stageResult: 'FAILURE') {
        sh 'ci/jenkins-reap.sh'
      }
      copyArtifacts projectName: env.JOB_NAME, selector: lastCompleted(), filter: 'allure-history/**',
                    target: 'previous', optional: true, fingerprintArtifacts: false
      catchError(buildResult: 'UNSTABLE', stageResult: 'FAILURE') {
        sh 'ci/jenkins-report.sh previous/allure-history'
      }
      junit testResults: 'junit/*.xml', allowEmptyResults: true,
            skipMarkingBuildUnstable: true, skipPublishingChecks: true
      publishHTML([reportName: 'Allure', reportDir: 'allure-report', reportFiles: 'index.html',
                   keepAll: true, alwaysLinkToLastBuild: true, allowMissing: true])
      publishHTML([reportName: 'CI summary', reportDir: 'ci-summary', reportFiles: 'index.html',
                   keepAll: true, alwaysLinkToLastBuild: true, allowMissing: true])
      script {
        // README badges (public via https://int21h.mooo.com/jenkins-static/
        // ralphd/ci/<branch>/<id>.svg; "build" is the plugin's own). Declared
        // on every build, so a build without counts shows "not run", not the
        // previous build's numbers.
        def testsBadge = addEmbeddableBadgeConfiguration(id: 'tests', subject: 'tests', status: 'not run', color: 'lightgrey')
        def lintBadge = addEmbeddableBadgeConfiguration(id: 'lint', subject: 'lint', status: 'not run', color: 'lightgrey')
        if (env.LINT_RESULT) {
          lintBadge.setStatus(env.LINT_RESULT)
          lintBadge.setColor(env.LINT_RESULT == 'passing' ? 'brightgreen' : 'red')
        }
        if (fileExists('ci-summary/counts.txt')) {
          def c = [:]
          readFile('ci-summary/counts.txt').split('\n').each { line ->
            def i = line.indexOf('=')
            if (i > 0) {
              c[line.substring(0, i)] = line.substring(i + 1).trim()
            }
          }
          if (c.pass) {
            def text = 'Tests: ' + c.pass + ' passed, ' + c.fail + ' failed, ' + c.skip + ' skipped, ' + c.flaky + ' flaky'
            def ok = c.fail == '0'
            testsBadge.setStatus(ok ? c.pass + ' passed' : c.fail + ' failed')
            testsBadge.setColor(ok ? 'brightgreen' : 'red')
            addSummary(icon: ok ? 'symbol-checkmark-circle-outline plugin-ionicons-api'
                                : 'symbol-close-circle-outline plugin-ionicons-api',
                       text: text + ' (<a href="' + env.BUILD_URL + 'Allure/">Allure</a>, ' +
                             '<a href="' + env.BUILD_URL + 'CI_20summary/">CI summary</a>)')
          }
        }
      }
      archiveArtifacts artifacts: 'ci-summary/**, allure-history/**, junit/**, .lanes/*/evidence/**',
                       allowEmptyArchive: true
    }
    // Every build posts one Telegram message, with the result, failed stage,
    // commit and test counts. Only a red or aborted main or nightly build
    // makes a sound; everything else arrives silently.
    cleanup {
      script {
        def watched = env.BRANCH_NAME == 'main' || env.CI_NIGHTLY == 'true'
        notifyTelegram(notifyOn: 'ALL', silent: watched ? null : true,
                       extra: env.CI_NIGHTLY == 'true' ? 'nightly (dev-layout cells)' : null)
      }
    }
  }
}
