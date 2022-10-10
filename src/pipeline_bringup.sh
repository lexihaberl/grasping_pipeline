#!/bin/bash

SESSION=VEREFINE
WS_PATH=~/demos

tmux -2 new-session -d -s $SESSION
tmux set -g mouse on

tmux new-window -t $SESSION:0 
tmux new-window -t $SESSION:1 

## with map setting
tmux select-window -t $SESSION:0
tmux split-window -h
tmux select-pane -t 0
tmux send-keys "source $WS_PATH/devel/setup.bash" C-m
tmux select-pane -t 1
tmux send-keys "source $WS_PATH/devel/setup.bash" C-m
tmux select-pane -t 0
tmux send-keys "roslaunch hsrb_moveit_config move_group.launch"
tmux send-keys enter
tmux select-pane -t 1
tmux send-keys "source $WS_PATH/devel/setup.bash" C-m
tmux send-keys "roslaunch grasping_pipeline statemachine.launch"
tmux send-keys enter
tmux select-window -t $SESSION:1
tmux send-keys "source $WS_PATH/devel/setup.bash" C-m
tmux send-keys "roslaunch haf_grasping haf_grasping_all.launch" C-m
tmux select-window -t $SESSION:0
tmux select-pane -t 2

tmux rename-window 'grasping'

# Attach to session
tmux -2 attach-session -t $SESSION
