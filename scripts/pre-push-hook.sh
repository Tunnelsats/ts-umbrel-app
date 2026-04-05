#!/bin/bash
# Pre-push hook to check for release promotion
# To install: ln -s ../../scripts/pre-push-hook.sh .git/hooks/pre-push

current_branch=$(git rev-parse --abbrev-ref HEAD)

# Only prompt on master branch
if [ "$current_branch" = "master" ] || [ "$current_branch" = "main" ]; then
    if [ ! -t 1 ]; then
        echo -e "\033[0;34m[INFO]\033[0m Non-interactive push detected. Skipping promotion prompt."
        echo -e "         Run 'npm run promote' manually if needed."
        exit 0
    fi

    exec < /dev/tty
    echo -e "\033[1;33m[NOTICE]\033[0m You are pushing to the main branch."
    read -r -p "Would you like to run 'npm run promote' to synchronize the official store PR before pushing? (y/N) " yn
    case $yn in
        [Yy]* ) 
            echo -e "\033[0;32m[INFO]\033[0m Running release promotion..."
            if ! npm run promote; then
                echo -e "\033[0;31m[ERROR]\033[0m Promotion failed. Push aborted."
                exit 1
            fi
            
            # Check for newly pinned digests
            if ! git diff --quiet -- tunnelsats/docker-compose.yml; then
                git add tunnelsats/docker-compose.yml
                git commit -m "chore(release): auto-pin SHA256 digest index via promote hook"
                echo -e "\033[1;33m[NOTICE]\033[0m Promotion committed new SHA256 immutable digest pins. The current push has been aborted to allow Git to recalculate the new HEAD."
                echo -e "         \033[0;32mPlease run 'git push' again.\033[0m"
                exit 1
            fi
            ;;
        * ) 
            echo -e "\033[0;34m[INFO]\033[0m Skipping promotion. Push proceeding..."
            ;;
    esac
fi

exit 0
