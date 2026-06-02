#!/bin/bash
#
# RALPH - Recursive Autonomous Loop for Project Handling for Job Finder
#
# Runs Claude Code in autonomous mode, executing one iteration at a time for
# Job Finder development. Uses RALPH.md as the guide and spec/tasks.md as the
# progress tracker.
#
# Usage:
#   ./ralph.sh              # Run full loop
#   ./ralph.sh --once       # Run single iteration
#   ./ralph.sh --dry-run    # Show what would be executed
#   ./ralph.sh --verbose    # Stream output live

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RALPH_PROMPT="$SCRIPT_DIR/RALPH.md"

# Configuration
MAX_ITERATIONS=30          # ~28 tasks in spec/tasks.md; a little headroom
ITERATION_DELAY=2
LOG_DIR="$SCRIPT_DIR/logs"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
GIT_BRANCH="main"          # adjust if your default branch differs

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Flags
SINGLE_RUN=false
DRY_RUN=false
VERBOSE=false
STOP_REQUESTED=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --once)
            SINGLE_RUN=true
            shift
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --verbose|-v)
            VERBOSE=true
            shift
            ;;
        -h|--help)
            echo "Usage: $0 [--once] [--dry-run] [--verbose] [-h|--help]"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

mkdir -p "$LOG_DIR"

# Patterns to detect secrets
SECRET_PATTERNS=(
    'api[_-]?key\s*[:=]'
    'api[_-]?secret\s*[:=]'
    'access[_-]?token\s*[:=]'
    'secret[_-]?key\s*[:=]'
    'private[_-]?key\s*[:=]'
    'password\s*[:=]'
    'auth[_-]?token\s*[:=]'
    'bearer\s+'
    'AKIA[0-9A-Z]{16}'
    'ghp_[0-9a-zA-Z]{36}'
    'adzuna[_-]?app[_-]?(id|key)\s*[:=]'   # project-specific: Adzuna credentials
)

log()     { echo -e "${BLUE}[$(date '+%Y-%m-%d %H:%M:%S')]${NC} $1"; }
success() { echo -e "${GREEN}[$(date '+%Y-%m-%d %H:%M:%S')] ✓${NC} $1"; }
warn()    { echo -e "${YELLOW}[$(date '+%Y-%m-%d %H:%M:%S')] ⚠${NC} $1"; }
error()   { echo -e "${RED}[$(date '+%Y-%m-%d %H:%M:%S')] ✗${NC} $1"; }

scan_for_secrets() {
    log "Scanning staged files for secrets..."
    local found=0
    local diff
    diff=$(git diff --cached --unified=0 2>/dev/null || true)
    for pattern in "${SECRET_PATTERNS[@]}"; do
        local matches
        matches=$(echo "$diff" | grep -iE "$pattern" || true)
        if [[ -n "$matches" ]]; then
            error "Possible secret detected (pattern: $pattern)"
            error "$matches"
            found=1
        fi
    done
    if [[ $found -eq 1 ]]; then
        error "Secrets scan failed. Aborting iteration."
        exit 1
    fi
    success "Secrets scan passed"
}

check_prerequisites() {
    log "Checking prerequisites..."
    if ! command -v claude &> /dev/null; then
        error "claude CLI not found. Install it from https://claude.ai/code"
        exit 1
    fi
    if ! command -v uv &> /dev/null; then
        error "uv not found. Install it: https://docs.astral.sh/uv/getting-started/installation/"
        exit 1
    fi
    if ! command -v git &> /dev/null; then
        error "git not found. Install git."
        exit 1
    fi
    if [[ ! -f "$RALPH_PROMPT" ]]; then
        error "RALPH.md not found at $RALPH_PROMPT"
        exit 1
    fi
    for f in spec/spec.md spec/hld.md spec/lld.md spec/tasks.md; do
        if [[ ! -f "$SCRIPT_DIR/$f" ]]; then
            error "Required spec file missing: $f"
            exit 1
        fi
    done
    success "Prerequisites ok"
}

# Verify the working tree builds/tests clean before starting a new iteration.
# RALPH.md forbids starting new work on a red pipeline; enforce it here too.
verify_clean_state() {
    if [[ "$DRY_RUN" == "true" ]]; then
        return 0
    fi
    cd "$SCRIPT_DIR"
    if ! git diff --quiet || ! git diff --cached --quiet; then
        warn "Uncommitted changes present before iteration — RALPH will reconcile per its protocol."
    fi
}

commit_and_push() {
    local iteration=$1
    log "Checking for changes to commit..."

    cd "$SCRIPT_DIR"

    # Stage all tracked modifications and new files, excluding env files and local data.
    # (Belt-and-suspenders: .gitignore should already exclude these.)
    git add --all -- ':!*.env' ':!.env*' ':!data' ':!data/**' ':!config/resume.*' 2>/dev/null || true

    if git diff --cached --quiet; then
        warn "No staged changes — skipping commit"
        return 0
    fi

    scan_for_secrets

    # Derive a commit subject from the most recent in-progress / completed task line.
    # tasks.md uses lines like:  "### T07 — Shared HTTP client ..."  with checkbox state
    # tracked separately, plus "- [~]"/"- [x]" markers. Grab the active task heading.
    local task_line
    task_line=$(grep -m1 -E '^\s*###\s+T[0-9]+' spec/tasks.md 2>/dev/null || echo "")
    local task_desc
    task_desc=$(echo "$task_line" | sed -E 's/^\s*###\s+//' | cut -c1-72)
    local commit_msg
    if [[ -n "$task_desc" ]]; then
        commit_msg="feat: ${task_desc}"
    else
        commit_msg="chore: RALPH iteration ${iteration} changes"
    fi

    git commit -m "$(cat <<EOF
${commit_msg}

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"

    # Push only if an 'origin' remote exists; otherwise stay local (first-run friendly).
    if git remote get-url origin &> /dev/null; then
        git push -u origin "$GIT_BRANCH"
        success "Changes committed and pushed"
    else
        warn "No 'origin' remote configured — committed locally only."
        warn "Add one with: git remote add origin <url>  (then this script will push)"
    fi
}

run_iteration() {
    local iteration=$1
    local log_file="$LOG_DIR/ralph_${TIMESTAMP}_iter${iteration}.log"
    log "Starting iteration $iteration. Log: $log_file"

    if [[ "$DRY_RUN" == "true" ]]; then
        log "[DRY RUN] Would execute claude with RALPH.md"
        return 0
    fi

    cd "$SCRIPT_DIR"

    if [[ "$VERBOSE" == "true" ]]; then
        claude --dangerously-skip-permissions -p "$(cat "$RALPH_PROMPT")" 2>&1 | tee "$log_file"
    else
        claude --dangerously-skip-permissions -p "$(cat "$RALPH_PROMPT")" > "$log_file" 2>&1
        log "--- Iteration $iteration output ---"
        cat "$log_file"
        log "--- End of iteration $iteration output ---"
    fi

    success "Iteration $iteration completed"
    commit_and_push "$iteration"
}

# Detect whether the project is finished: the final verification task (T28) is
# marked complete in tasks.md. When true, the loop can stop early.
project_complete() {
    grep -qE '^\s*-?\s*\[x\].*T28' "$SCRIPT_DIR/spec/tasks.md" 2>/dev/null
}

handle_interrupt() {
    if [[ "$STOP_REQUESTED" == "true" ]]; then
        warn "Force quit"
        exit 130
    fi
    warn "Ctrl+C detected — iteration will finish first"
    STOP_REQUESTED=true
}
trap 'handle_interrupt' INT

main() {
    echo "Starting RALPH autonomous loop for Job Finder"
    check_prerequisites
    verify_clean_state

    if [[ "$SINGLE_RUN" == "true" ]]; then
        run_iteration 1
        exit 0
    fi

    for ((i=1;i<=MAX_ITERATIONS;i++)); do
        run_iteration "$i"

        if project_complete; then
            success "T28 complete — project is done. Stopping loop."
            break
        fi

        if [[ "$STOP_REQUESTED" == "true" ]]; then
            warn "Stop requested — exiting after iteration $i"
            break
        fi

        if [[ $i -lt $MAX_ITERATIONS ]]; then
            log "Waiting $ITERATION_DELAY seconds before next iteration..."
            sleep "$ITERATION_DELAY" || true
        fi
    done

    success "RALPH loop finished"
}

main "$@"
