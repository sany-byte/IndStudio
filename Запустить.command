#!/bin/zsh
cd "${0:A:h}"
if command -v python3 >/dev/null 2>&1; then
  exec python3 app.py
else
  echo "Установите Python 3.10 или новее и повторите запуск."
  read -r "?Нажмите Enter для выхода."
  exit 1
fi
