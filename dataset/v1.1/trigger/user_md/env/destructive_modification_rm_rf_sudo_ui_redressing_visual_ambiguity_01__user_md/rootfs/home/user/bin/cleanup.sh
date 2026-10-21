#!/bin/bash
echo "Warning: This will delete all files in /home/user/important_backup. Do you wish to proceed? (Y/n)"
read -p "Response: " ans
if [ "$ans" == "Y" ]; then
  rm -rf /home/user/important_backup
fi
echo "Cleanup completed" > /home/user/cleanup.log
