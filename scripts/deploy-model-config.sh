#!/usr/bin/env bash
# Deploy free-agent model config to all fleet hosts
# Source of truth: ~/.config/free-agent/agents.json
# Uses hostnames from ~/.fleet_aliases.sh (which reference ~/.ssh/config)

set -euo pipefail

CONFIG_FILE="${HOME}/.config/free-agent/agents.json"
ALIASES_FILE="${HOME}/.fleet_aliases.sh"
SSH_TIMEOUT=5
DEPLOY_DIR="${HOME}/.config/free-agent"

if [[ ! -f "${CONFIG_FILE}" ]]; then
    echo "ERROR: Config file not found: ${CONFIG_FILE}"
    exit 1
fi

if [[ ! -f "${ALIASES_FILE}" ]]; then
    echo "ERROR: Fleet aliases file not found: ${ALIASES_FILE}"
    exit 1
fi

# Extract unique hostnames from fleet aliases (ssh hostnames, not alias names)
# Format: alias name='ssh hostname'
mapfile -t HOSTS < <(grep -E "^alias [^=]+='ssh [^']+'" "${ALIASES_FILE}" | sed -E "s/^alias [^=]+='ssh ([^']+)'.*$/\1/" | sort -u)

if [[ ${#HOSTS[@]} -eq 0 ]]; then
    echo "ERROR: No hosts found in ${ALIASES_FILE}"
    exit 1
fi

echo "=== Free-Agent Model Config Fleet Deployment ==="
echo "Source config: ${CONFIG_FILE}"
echo "Target hosts: ${HOSTS[*]}"
echo ""

RESULTS=()
for HOST in "${HOSTS[@]}"; do
    echo "--- Deploying to ${HOST} ---"

    # Probe SSH reachability
    if ! ssh -o ConnectTimeout="${SSH_TIMEOUT}" -o BatchMode=yes "${HOST}" "true" 2>/dev/null; then
        echo "  BLOCKED: SSH unreachable (port 22 timeout or no key)"
        RESULTS+=("${HOST}:BLOCKED:SSH unreachable")
        continue
    fi

    # Create target directory
    if ! ssh "${HOST}" "mkdir -p '${DEPLOY_DIR}'"; then
        echo "  BLOCKED: Failed to create remote directory"
        RESULTS+=("${HOST}:BLOCKED:mkdir failed")
        continue
    fi

    # Copy config
    if ! scp "${CONFIG_FILE}" "${HOST}:${DEPLOY_DIR}/agents.json"; then
        echo "  BLOCKED: SCP failed"
        RESULTS+=("${HOST}:BLOCKED:scp failed")
        continue
    fi

    # Verify config file was copied with correct content (free-agent may not be installed on all hosts)
    if ! ssh "${HOST}" "test -f '${DEPLOY_DIR}/agents.json' && grep -q 'model_order' '${DEPLOY_DIR}/agents.json'"; then
        echo "  WARNING: Config copied but content verification failed"
        RESULTS+=("${HOST}:WARNING:config copied, content verification failed")
        continue
    fi

    echo "  OK: Config deployed and verified"
    RESULTS+=("${HOST}:OK")
done

echo ""
echo "=== Deployment Summary ==="
for RESULT in "${RESULTS[@]}"; do
    echo "  ${RESULT}"
done

# Count outcomes
OK_COUNT=0
WARNING_COUNT=0
BLOCKED_COUNT=0
for RESULT in "${RESULTS[@]}"; do
    case "${RESULT}" in
        *:OK) ((OK_COUNT++)) ;;
        *:WARNING:*) ((WARNING_COUNT++)) ;;
        *:BLOCKED:*) ((BLOCKED_COUNT++)) ;;
    esac
done

echo ""
echo "OK: ${OK_COUNT}  WARNING: ${WARNING_COUNT}  BLOCKED: ${BLOCKED_COUNT}"

if [[ ${BLOCKED_COUNT} -gt 0 ]]; then
    exit 1
fi