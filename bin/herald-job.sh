#!/bin/zsh
# Entry point for Jarvis's scheduler (job kind "script", args = the herald subcommand):
# Jarvis keeps the clock, Herald does the work. `herald-job.sh asana-digest` etc.
exec $HOME/.herald/bin/herald "$@"
