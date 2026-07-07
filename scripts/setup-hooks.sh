#!/bin/bash
# Install git hooks for local CI enforcement.
# Run once after clone: bash scripts/setup-hooks.sh

set -e

HOOKS_DIR="$(git rev-parse --git-dir)/hooks"
PROJECT_ROOT="$(git rev-parse --show-toplevel)"

echo "Installing pre-commit hook..."

cat > "$HOOKS_DIR/pre-commit" << 'EOF'
#!/bin/bash
# Pre-commit hook: runs local CI checks before allowing commit
# Exits non-zero if any check fails, blocking the commit

set -e

echo "Running pre-commit checks..."
echo ""

# Run make check
make check

# If we get here, all checks passed
echo ""
echo "✓ All pre-commit checks passed!"
EOF

chmod +x "$HOOKS_DIR/pre-commit"

echo "✓ Pre-commit hook installed at $HOOKS_DIR/pre-commit"
echo ""
echo "The hook will run 'make check' (ruff + mypy + pytest) before each commit."
