#!/usr/bin/env python3
"""Wrapper to run alt data agent from crontab."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ml_alt_data_agent import run
run()
