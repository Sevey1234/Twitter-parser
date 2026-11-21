from enum import Enum


class State(str, Enum):
    USERNAME = "username"
    CONFIRM = "confirm"
    INTERVAL = "interval"
    BUDGET = "budget"
    ACTION = "author_action"
    REMOVE = "confirm_remove"
    LABEL = "label"
    RESET = "reset"
