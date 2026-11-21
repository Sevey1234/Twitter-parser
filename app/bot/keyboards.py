from telebot import types

CANCEL = "Отмена"
YES = "Подтвердить"


def choices(labels):
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True, row_width=2)
    keyboard.add(*(types.KeyboardButton(label) for label in labels))
    keyboard.row(CANCEL)
    return keyboard


def close():
    return types.ReplyKeyboardRemove()
