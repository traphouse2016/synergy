# DEFAULT_BASE_BUILD: fixed46_press1_tg_filter_vm_fastfail_contacts_parser
#!/usr/bin/env python3
"""Synergy 1.0 — FFT-based DTMF detection via Web Audio CDP injection"""

import os, json, time, re, threading, logging, subprocess, asyncio, tempfile, shutil
from datetime import datetime
import requests
from flask import Flask, jsonify, request
from flask_cors import CORS
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from webdriver_manager.core.os_manager import ChromeType
from selenium.common.exceptions import WebDriverException
