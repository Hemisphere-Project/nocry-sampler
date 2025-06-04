#!/bin/bash

BASEPATH="$(dirname "$(readlink -f "$0")")"

echo "$BASEPATH"
cd "$BASEPATH"

## xBIAN (DEBIAN / RASPBIAN / UBUNTU)
if [[ $(command -v apt) ]]; then
    DISTRO='xbian'
    echo "Distribution: $DISTRO"

    apt install libsndfile1-dev portaudio19-dev libportmidi-dev liblo-dev -y

## ARCH Linux
elif [[ $(command -v pacman) ]]; then
    DISTRO='arch'
    echo "Distribution: $DISTRO"

    pacman -S libsndfile1-dev portaudio19-dev libportmidi-dev liblo-dev --noconfirm --needed

## Plateform not detected ...
else
    echo "Distribution not detected:"
    echo "this script needs APT or PACMAN to run."
    echo ""
    echo "Please install manually."
    exit 1
fi

python3 -m venv venv
source venv/bin/activate
pip install pyo
# pip install pygame

ln -sf "$BASEPATH/nocry.service" /etc/systemd/system/
ln -sf "$BASEPATH/nocry" /usr/local/bin/

systemctl daemon-reload

mkdir -p /data/var/nocry
cp "$BASEPATH/sampler_config.json" /data/var/nocry/config.json

FILE=/boot/starter.txt
if test -f "$FILE"; then
echo "## [nocry] midi sampler
# nocry
" >> /boot/starter.txt
fi