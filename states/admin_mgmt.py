from aiogram.dispatcher.filters.state import State, StatesGroup


class AdminAdd(StatesGroup):
    waiting_for_id = State()
