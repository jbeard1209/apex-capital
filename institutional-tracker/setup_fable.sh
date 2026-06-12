#!/bin/bash
# APEX CAPITAL — Fable 5 Setup
# Run this once to configure your Anthropic API key.
# After June 22, edit config/llm_config.json and change "model" to "claude-opus-4-8"

echo ""
echo "=== Apex Capital — Fable 5 Setup ==="
echo ""

# Prompt for API key
read -s -p "Paste your Anthropic API key: " API_KEY
echo ""

if [ -z "$API_KEY" ]; then
    echo "No key entered. Exiting."
    exit 1
fi

# Add to shell profile
SHELL_RC="$HOME/.zshrc"
if [ -f "$HOME/.bashrc" ]; then
    SHELL_RC="$HOME/.bashrc"
fi

# Remove any existing entry and add fresh
grep -v "ANTHROPIC_API_KEY" "$SHELL_RC" > /tmp/_rc_tmp && mv /tmp/_rc_tmp "$SHELL_RC"
echo "export ANTHROPIC_API_KEY=\"$API_KEY\"" >> "$SHELL_RC"
export ANTHROPIC_API_KEY="$API_KEY"

echo "✓ Key saved to $SHELL_RC"
echo ""

# Test the key
echo "Testing key against claude-fable-5..."
python3 - << 'EOF'
import os, sys
sys.path.insert(0, '.')
os.environ.get('ANTHROPIC_API_KEY', '')
from apex_llm import reason, is_available, get_model
if not is_available('rd_pod_board'):
    print("  Key not detected — check your shell profile")
    sys.exit(1)
result = reason(
    system="You are a test agent.",
    user="Reply with exactly: FABLE_OK",
    agent_name="rd_pod_board",
    max_tokens=20,
)
if result and "FABLE" in result:
    print(f"  ✓ {get_model()} is working!")
elif result:
    print(f"  ✓ API connected (unexpected reply: {result[:60]})")
else:
    print("  ✗ TEST FAILED — key did not authenticate or has no credits.")
    print("    Common causes: key pasted with extra characters, or no usage")
    print("    credits purchased (console.anthropic.com → Plans & Billing).")
    sys.exit(1)
EOF

echo ""
echo "=== Done ==="
echo ""
echo "Board members will now use $( python3 -c "import json; print(json.load(open('config/llm_config.json'))['model'])" ) for reasoning."
echo ""
echo "After June 22, run:"
echo "  python3 -c \"import json; c=json.load(open('config/llm_config.json')); c['model']='claude-opus-4-8'; json.dump(c, open('config/llm_config.json','w'), indent=2)\""
echo ""
