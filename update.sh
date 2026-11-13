#!/bin/bash
# Плановый сбор всех подборок и выкладка на GitHub Pages. Запускается таймером hh-panel.timer.
set -e
cd "$(dirname "$0")"
[ -f ~/.config/hh-panel.env ] && set -a && . ~/.config/hh-panel.env && set +a   # HH_TG_TOKEN / HH_TG_CHAT
python3 collector.py --publish
