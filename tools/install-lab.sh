#!/bin/sh
# Разложить команды песочницы в /usr/local/bin. Идемпотентно.
#
# Ссылки, а не копии: правка скрипта в репозитории сразу действует, и её видно
# в истории. Запускать после `git pull` в /opt/outreach49-lab.
set -e
LAB=/opt/outreach49-lab
for name in o49lab o49live o49sql o49refresh o49test; do
    chmod +x "$LAB/tools/$name"
    ln -sfn "$LAB/tools/$name" "/usr/local/bin/$name"
    printf '%s -> %s\n' "/usr/local/bin/$name" "$LAB/tools/$name"
done
