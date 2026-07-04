from aiogram.dispatcher.filters.state import State, StatesGroup


class ViolationsFlow(StatesGroup):
    period = State()
    event_type = State()
    top10 = State()
