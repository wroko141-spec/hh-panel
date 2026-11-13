#!/bin/bash
# Собирает вакансии и выкладывает их на GitHub Pages. Запускается таймером hh-panel.timer.
set -e
cd "$(dirname "$0")"
[ -f ~/.config/hh-panel.env ] && set -a && . ~/.config/hh-panel.env && set +a   # HH_TG_TOKEN / HH_TG_CHAT
python3 collector.py
git add data
git diff --cached --quiet && exit 0
git -c user.name=wroko141-spec -c user.email=wroko141-spec@users.noreply.github.com commit -qm "Данные $(TZ=Europe/Moscow date '+%d.%m %H:%M')"
H=$(printf 'x-access-token:%s' "$(cat ~/.github_token)" | base64 -w0)
git -c "http.extraHeader=Authorization: Basic $H" push -q https://github.com/wroko141-spec/hh-panel.git main
