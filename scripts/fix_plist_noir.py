#!/usr/bin/env python3
"""修复 launchd plist — 确保 SERENITY_THEME=terminal-noir 正确写入"""
import plistlib
import pathlib

p = pathlib.Path.home() / 'Library/LaunchAgents/com.serenity.dashboard.plist'
d = plistlib.loads(p.read_bytes())
d['EnvironmentVariables'] = {'SERENITY_THEME': 'terminal-noir'}
p.write_bytes(plistlib.dumps(d))
print('✅ plist 已修复: SERENITY_THEME=terminal-noir')
