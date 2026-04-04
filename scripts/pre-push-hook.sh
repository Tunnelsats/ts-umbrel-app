#!/bin/bash
# Pre-push hook to check for release promotion
# To install: ln -s ../../scripts/pre-push-hook.sh .git/hooks/pre-push

current_branch=$(git branch --show-current)

# Only prompt on master branch
if [ "$current_branch" = "master" ] || [ "$current_branch" = "main" ]; then
    # Use standard input explicitly since hooks don't always run with a TTY
    exec < /dev/tty
    echo -e "\033[1;33m[NOTICE]\033[0m You are pushing to the main branch."
    read -p "Would you like to run 'npm run promote' to synchronize the official store PR before pushing? (y/N) " yn
    case $yn in
        [Yy]* ) 
            echo -e "\033[0;32m[INFO]\033[0m Running release promotion..."
            npm run promote
            if [ $? -ne 0 ]; then
                echo -e "\033[0;31m[ERROR]\033[0m Promotion failed. Push aborted."
                exit 1
            fi
            ;;
        * ) 
            echo -e "\033[0;34m[INFO]\033[0m Skipping promotion. Push proceeding..."
            ;;
    esac
fi

exit 0
