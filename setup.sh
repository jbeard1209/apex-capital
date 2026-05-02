#!/bin/bash
# =============================================================================
# SETUP SCRIPT — Institutional Tracker
# Run this once to install all dependencies
# =============================================================================

echo ""
echo "=================================================="
echo "   INSTITUTIONAL TRACKER — SETUP"
echo "=================================================="
echo ""

# Check Python version
python3 --version
if [ $? -ne 0 ]; then
    echo "❌ Python 3 is required. Please install Python 3.10+"
    exit 1
fi

echo ""
echo "📦 Installing Python dependencies..."
pip3 install -r requirements.txt

echo ""
echo "✅ Dependencies installed."
echo ""
echo "=================================================="
echo "   NEXT STEPS:"
echo "=================================================="
echo ""
echo "1. Open config.py and fill in:"
echo "   - ALPACA_API_KEY and ALPACA_SECRET_KEY"
echo "   - EMAIL_SENDER (your Gmail)"
echo "   - EMAIL_RECIPIENT (your Gmail)"
echo "   - GMAIL_APP_PASSWORD (16-char App Password)"
echo ""
echo "2. Test your email setup:"
echo "   python3 email_sender.py"
echo ""
echo "3. Run the 5-year backtest:"
echo "   python3 run_backtest.py"
echo ""
echo "4. Run a single daily paper trading day manually:"
echo "   python3 main.py"
echo ""
echo "5. The daily scheduled task will run automatically at 4:15 PM ET."
echo ""
