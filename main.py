import logging
import asyncio
import os
import random
import re
from random import shuffle
import sqlite3
from typing import List

from aiogram import Bot, Dispatcher, types, md
from aiogram.dispatcher.filters import Text
from aiogram.utils import exceptions, executor
from safe_schedule import SafeScheduler
import time
from datetime import datetime, time, timedelta
import psycopg
from aiogram.utils.markdown import escape_md
from psycopg.rows import dict_row

from constants import HELLO_MESSAGE, LEARNING_SOURCES, FEEDBACK, STATISTICS, transcribe_az_dict, ADM_HELP, Keyboard
from secrets import TELEGRAM_API_TOKEN, POSTGRE_USER, POSTGRE_PWD

path = os.path.dirname(os.path.abspath(__file__))
conn = psycopg.connect(dbname=POSTGRE_DB_NAME, user=POSTGRE_USER, password=POSTGRE_PWD, host='localhost')
conn.row_factory = dict_row
c = conn.cursor()

# Включаем логирование, чтобы не пропустить важные сообщения
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s %(levelname)s:%(name)s]: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
# Объект бота
bot = Bot(token=TELEGRAM_API_TOKEN, parse_mode=types.ParseMode.MARKDOWN_V2)
# Диспетчер
dp = Dispatcher(bot)

USER_IDS_TO_SEND_MESSAGES_TO = [127869357, 5632448031]
MORNING_TIME = '05:30'
EVENING_TIME = '15:30'
SCHEDULE = [MORNING_TIME, EVENING_TIME]

ADMINS = [127869357, 5632448031]
ADMINS_ALL = [1, 9, 127869357, 5632448031]


@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    tg_id = message.from_user.id
    username = message.from_user.username
    first_name = message.from_user.first_name
    now = datetime.now()
    c.execute(
        '''INSERT INTO users (tg_id, username, first_name, registration_date)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (tg_id) DO UPDATE SET is_blocked = FALSE''',
        (tg_id, username, first_name, now)
    )
    conn.commit()
    id = c.execute('SELECT id FROM users WHERE users.tg_id = %s', (tg_id,))
    user_id = id.fetchone()['id']
    words = c.execute('SELECT * FROM vocabulary WHERE level = 1')
    words = words.fetchall()
    logging.info(f"Words to add for {tg_id}: {words}")
    for word in words:
        c.execute(
            '''INSERT INTO user_vocabulary (user_id, vocabulary_id, correct_answer_id, num_right_guesses, poll_id)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING''',
            (user_id, word["id"], -1, 0, None)
        )
        conn.commit()
    logging.info(f"Registered user {tg_id}")
    await message.answer(HELLO_MESSAGE, reply_markup=default_menu(user_id))
    await send_alphabet(message)
    await send_messages(message.from_user.id)


@dp.message_handler(commands=['learn_now'])
async def words_now(message: types.Message):
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    buttons = ["Ещё слово"]
    keyboard.add(*buttons)
    await send_messages(message.chat.id)


@dp.message_handler(commands=['adm_message'])
async def send_to_all(message: types.Message):
    logging.info(f'Got admin broadcast message {message.md_text}')
    c.execute('SELECT DISTINCT tg_id FROM users WHERE is_blocked = false')
    ids = [row["tg_id"] for row in c.fetchall()]
    tg_id = message.from_user.id
    if tg_id in ADMINS:
        broadcast_message = message.md_text.replace('/adm\\_message ', '')
        for id in ids:
            await bot.send_message(
                chat_id=id,
                text=broadcast_message,
            )


@dp.message_handler(Text('Ещё слово'))
async def more_words(message: types.Message):
    await words_now(message)


@dp.message_handler(Text('Произношение букв'))
async def more_words(message: types.Message):
    await send_alphabet(message)


@dp.message_handler(Text('Ресурсы по изучению 🇦🇿'))
async def more_words(message: types.Message):
    await message.reply(text=LEARNING_SOURCES)


@dp.message_handler(Text('Предложения по боту'))
async def more_words(message: types.Message):
    await message.reply(text=FEEDBACK)


@dp.message_handler(Text('Мой прогресс'))
async def my_progress(message: types.Message):
    user_ids = [message.from_user.id]
    await send_statistics_by_ids(user_ids)


@dp.message_handler(Text(Keyboard.ADM_STAT.value))
async def adm_statistics(message: types.Message):
    tg_id = message.from_user.id
    if tg_id not in ADMINS_ALL:
        return
    stat = 'Топ за вчера: \n\n'
    top = c.execute(
        '''SELECT MAX(level) as max_level, COUNT(word_az) as words_learned, (CASE WHEN (username IS NULL OR username = '') THEN tg_id::name ELSE username END) as user FROM user_vocabulary
        JOIN users ON user_vocabulary.user_id = users.id
        JOIN vocabulary ON user_vocabulary.vocabulary_id = vocabulary.id
        WHERE NOW() - last_send <= interval '24 hours'
        GROUP BY username, tg_id
        ORDER BY words_learned DESC
        LIMIT 20'''
    ).fetchall()
    for top_man in top:
        stat += f'@{top_man["user"]} - {top_man["words_learned"]} слов (Уровень {top_man["max_level"]})\n'
    stat += '\nНедавно зарегистрировавшиеся:\n'
    fresh_registered = c.execute(
        '''
            SELECT (CASE WHEN (username IS NULL OR username = '') THEN tg_id::name ELSE username END) as user, first_name, registration_date, is_blocked
            FROM users
            ORDER BY registration_date DESC
            LIMIT 10
        '''
    ).fetchall()
    for registered in fresh_registered:
        stat += f'@{registered["user"]} {registered["first_name"]} - {registered["registration_date"]}. Блок: {registered["is_blocked"]}.\n'
    await message.answer(text=stat, parse_mode=types.ParseMode.HTML)


@dp.message_handler(Text(Keyboard.ADM_HELP.value))
async def adm_help(message: types.Message):
    tg_id = message.from_user.id
    if tg_id not in ADMINS_ALL:
        return
    await message.answer(
        text=ADM_HELP,
    )


@dp.message_handler(Text('Уроки грамматики'))
async def more_words(message: types.Message):
    lessons = c.execute(
        '''
        SELECT * FROM lessons
        ORDER BY learn_order ASC
        '''
    )
    reply_text = ''
    for lesson in lessons:
        reply_text += f'{lesson["name"]} /lesson_{lesson["id"]}\n'
    await message.answer(text=escape_md(reply_text))


@dp.message_handler(Text(startswith='/lesson_'))
async def more_words(message: types.Message):
    lesson_id = re.search(r'\/lesson_(\d+)', message.text).group(1)
    lessons = c.execute(
        '''
        SELECT * FROM lessons
        WHERE id = %s
        ORDER BY learn_order ASC
        ''',
        (lesson_id,)
    )
    reply_text = ''
    for lesson in lessons:
        reply_text += f'{lesson["link"]}\n'
    await message.answer(text=escape_md(reply_text))


@dp.callback_query_handler()
async def more_words(callback_query: types.CallbackQuery):
    pattern = r'([^\s]{1,}) .*'
    parsed = re.search(pattern, callback_query.data)
    action = parsed.group(1)
    if action == 'learn_now':
        pattern = r'([^\s]{1,}) ([^\s]{1,}) ([^\s]{1,})'
        parsed = re.search(pattern, callback_query.data)
        user_id = parsed.group(2)
        word_id = parsed.group(3)
        c.execute(
            '''
            UPDATE user_vocabulary
            SET num_right_guesses = 10, poll_id = NULL, correct_answer_id = -1
            WHERE user_id = %s AND vocabulary_id = %s
            ''',
            (user_id, word_id)
        )
        conn.commit()
        await callback_query.message.answer('Слово помечено выученным')
        await bot.delete_message(callback_query.message.chat.id, callback_query.message.message_id)
        await words_now(callback_query.message)

    if action == 'learn_more':
        logging.info(f"more words {callback_query.message}")
        await words_now(callback_query.message)


# Define a function to send the messages
@dp.message_handler()
async def send_messages(id: int = None, fast: bool = False):
    c.execute('SELECT DISTINCT tg_id FROM users WHERE is_blocked = false')
    USER_IDS_TO_SEND_MESSAGES_TO = [row["tg_id"] for row in c.fetchall()]
    ids_to_send = [id] if id else USER_IDS_TO_SEND_MESSAGES_TO
    logging.info(f"Checking messages to {ids_to_send}")
    for user_id in ids_to_send:
        try:
            word_translation = []
            az_ru_quiz = []
            ru_az_quiz = []
            words = c.execute(
                '''SELECT word_az, word_ru, word_emoji, user_id, vocabulary_id, num_right_guesses, last_send FROM user_vocabulary
                JOIN users ON user_vocabulary.user_id = users.id
                JOIN vocabulary ON user_vocabulary.vocabulary_id = vocabulary.id
                WHERE users.tg_id = %s AND user_vocabulary.num_right_guesses < 10
                ORDER BY random()''',
                (user_id,)
            )
            words = words.fetchall()
            user_in_voc_id = words[0]['user_id']
            for word in words:
                if word['num_right_guesses'] < 2:
                    word_translation.append(word)
                elif word['num_right_guesses'] > 7:
                    ru_az_quiz.append(word)
                else:
                    az_ru_quiz.append(word)
            logging.info(f'User {user_id} ru_az:{len(ru_az_quiz)}, az_ru:{len(az_ru_quiz)}, plain:{len(word_translation)}')
            logging.info(f'User {user_id} learned {len(get_asked_words(words))} words today')
            if len(get_asked_words(words)) >= 50:
                await bot.send_message(
                    chat_id=user_id,
                    text=md.escape_md(f'''За последние 6 часов было показано 50 слов! Отдохните и возвращайтесь позже 🙃'''),
                    reply_markup=default_menu(user_id)
                )
                continue
            if len(ru_az_quiz) > 10:
                ru_az_quiz = get_unasked_words(ru_az_quiz)
                if len(ru_az_quiz) > 0:
                    await translation_quiz(user_id, words, ru_az_quiz, 'word_ru', 'word_az', is_fast=fast)
                    continue
            if len(az_ru_quiz) > 20:
                az_ru_quiz = get_unasked_words(az_ru_quiz)
                if len(az_ru_quiz) > 0:
                    await translation_quiz(user_id, words, az_ru_quiz, 'word_az', 'word_ru', is_fast=fast)
                    continue
            if len(word_translation) > 20:
                word_translation = get_unasked_words(word_translation)
                if len(word_translation) >= 5:
                    await new_words_message(user_id, user_in_voc_id, word_translation)
                    continue
            new_words = add_new_words_for_user(user_in_voc_id)
            check_old_words(user_in_voc_id)
            if len(new_words) >= 5:
                await new_words_message(user_id, user_in_voc_id, new_words)
            else:
                await bot.send_message(
                    chat_id=user_id,
                    text=md.escape_md(f'''На текущий момент это все слова, которые есть в боте, хорошая работа! Скоро будут новые наборы :)'''),
                    reply_markup=default_menu(user_id)
                )
                continue

        except exceptions.BotBlocked:
            logging.warning(f"Bot was blocked by user {user_id}")
            c.execute(
                '''
                UPDATE users
                SET is_blocked = true
                WHERE tg_id = %s
                ''',
                (user_id,)
            )
            await asyncio.sleep(1)
        except exceptions.ChatNotFound:
            logging.warning(f"Chat not found for user {user_id}")
        except exceptions.RetryAfter as e:
            logging.warning(f"Rate limited. Sleeping for {e.timeout} seconds")
            await asyncio.sleep(e.timeout)
        except exceptions.TelegramAPIError as e:
            logging.exception(f"Failed to send message to user {user_id}. Exception: {e}")
        except Exception as e:
            logging.exception(f"Something happened: {e}")


@dp.poll_answer_handler()
async def poll_answer(poll_answer: types.PollAnswer):
    poll_id = poll_answer.poll_id
    answer_ids = poll_answer.option_ids
    answers = c.execute(
        '''SELECT correct_answer_id FROM user_vocabulary
        WHERE poll_id = %s''',
        (poll_id,)
    )
    answer = answers.fetchall()[0]['correct_answer_id']
    logging.info(f'Poll answer! Got {answer_ids[0]}, right is {answer}! You are {answer == answer_ids[0]}')
    dt = datetime.now()
    if answer == answer_ids[0]:
        c.execute(
            '''
            UPDATE user_vocabulary
            SET num_right_guesses = num_right_guesses + 1, poll_id = NULL, correct_answer_id = -1, last_send = %s
            WHERE
            poll_id = %s
            ''',
            (dt, poll_id,)
        )
    else:
        c.execute(
            '''
            UPDATE user_vocabulary
            SET num_right_guesses = num_right_guesses - 1, poll_id = NULL, correct_answer_id = -1, last_send = %s
            WHERE
            poll_id = %s
            ''',
            (dt, poll_id,)
        )
    conn.commit()
    await send_messages(poll_answer.user.id, fast=True)


async def scheduler():
    schedule = SafeScheduler()
    schedule.every().day.at("05:30").do(send_statistics_by_ids, ids=[])
    for time in SCHEDULE:
        schedule.every().day.at(time).do(send_messages)
    while True:
        await schedule.run_pending()
        await asyncio.sleep(1)


async def on_startup(dp):
    asyncio.create_task(scheduler())


async def translation_quiz(user_id, words: List, right_words: List, from_lang: str, to_lang: str, is_fast: bool = False):
    right_word = random.choice(right_words)
    logging.info(f'Word for user {user_id}: {right_word}')
    words = list(filter(lambda word: word['word_ru'] != right_word['word_ru'], words))
    wrong_words = words[:3]
    answers = [word[to_lang] for word in wrong_words] + [right_word[to_lang]]
    shuffle(answers)
    right_answer_index = answers.index(right_word[to_lang])
    keyboard = types.InlineKeyboardMarkup()
    button = types.InlineKeyboardButton(
        text=f"Я уже знаю это слово",
        callback_data=f'learn_now {right_word["user_id"]} {right_word["vocabulary_id"]}'
    )
    keyboard.add(button)
    button = types.InlineKeyboardButton(
        text=f"Ещё слово",
        callback_data=f'learn_more '
    )
    keyboard.add(button)
    poll_params = {
        'chat_id': user_id,
        'question': f'{right_word[from_lang]}',
        'options': answers,
        'is_anonymous': False,
        'type': 'quiz',
        'correct_option_id': right_answer_index,
        'reply_markup': keyboard
    }
    if is_fast:
        poll_params['open_period'] = 60
    message_poll_id: types.Message = await bot.send_poll(**poll_params)
    logging.info(f'Message poll: {message_poll_id.poll.id}')
    c.execute(
        '''
        UPDATE user_vocabulary
        SET correct_answer_id = %s, poll_id = %s
        WHERE
        user_id = %s AND vocabulary_id = %s
        ''',
        (right_answer_index, message_poll_id.poll.id, right_word['user_id'], right_word['vocabulary_id'],)
    )
    conn.commit()


async def new_words_message(user_id, internal_user_id, learn_words: List):
    message = ''
    learn_words = learn_words[:5]
    keyboard = types.InlineKeyboardMarkup()
    button = types.InlineKeyboardButton(
        text=f"Ещё слово",
        callback_data=f'learn_more '
    )
    keyboard.add(button)
    menu_keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    menu_keyboard.add("Ещё слово", "Произношение букв")
    dt = datetime.now()
    for word in learn_words:
        if word['num_right_guesses'] == -1:
            escaped = escape_md(f'{word["word_emoji"]} {word["word_ru"]} - {word["word_az"]} [{get_transcription(word)}]')
        else:
            escaped = f'{escape_md(word["word_emoji"])} {escape_md(word["word_ru"])} \- ||{escape_md(word["word_az"])} \[{escape_md(get_transcription(word))}\]||'
        message += f'{escaped}\n'
        c.execute(
            '''
            UPDATE user_vocabulary
            SET num_right_guesses = num_right_guesses + 1, poll_id = NULL, correct_answer_id = -1, last_send = %s
            WHERE
            user_id = %s AND vocabulary_id = %s
            ''',
            (dt, internal_user_id, word['vocabulary_id'])
        )
        conn.commit()
    await bot.send_message(user_id, text=message, reply_markup=keyboard)


async def send_alphabet(message: types.Message):
    await bot.send_photo(
        message.chat.id,
        photo='https://legkonauchim.ru/wp-content/uploads/2020/12/transkriptsiya-alfavita.gif',
        caption=md.escape_md('Начнём с алфавита. Постарайтесь прочитать азербайджанские слова с этой шпаргалкой. Так же можно воспользоваться более подробным уроком на нашем сайте https://lessons.baku-aio.ru/azerbaydzhanskiy-alfavit :)')
    )


async def send_statistics_by_ids(ids):
    if not len(ids):
        # ids = ADMINS
        c.execute('SELECT DISTINCT tg_id FROM users WHERE is_blocked = false')
        ids = [row["tg_id"] for row in c.fetchall()]
    for user_id in ids:
        try:
            logging.info(f'Getting statistics for {user_id}')
            start_learning = []
            active_learning = []
            learned_words = []
            max_level = 0
            words = c.execute(
                '''SELECT num_right_guesses, level FROM user_vocabulary
                JOIN users ON user_vocabulary.user_id = users.id
                JOIN vocabulary ON user_vocabulary.vocabulary_id = vocabulary.id
                WHERE users.tg_id = %s AND user_vocabulary.num_right_guesses >= 0
                ORDER BY random()''',
                (user_id,)
            )
            words = words.fetchall()
            for word in words:
                if word['num_right_guesses'] >= 10:
                    learned_words.append(word)
                elif word['num_right_guesses'] >= 2:
                    active_learning.append(word)
                else:
                    start_learning.append(word)
            max_level = max([word['level'] for word in words])
            message_to_send = STATISTICS.format(max_level, len(learned_words), len(active_learning), len(start_learning))
            logging.info(f'Statistics for {user_id}: {message_to_send}')
            await bot.send_message(
                chat_id=user_id,
                text=message_to_send,
                reply_markup=default_menu(user_id)
            )
        except exceptions.BotBlocked:
            logging.warning(f"Bot was blocked by user {user_id}")
            await asyncio.sleep(1)
        except exceptions.ChatNotFound:
            logging.warning(f"Chat not found for user {user_id}")
        except exceptions.RetryAfter as e:
            logging.warning(f"Rate limited. Sleeping for {e.timeout} seconds")
            await asyncio.sleep(e.timeout)
        except exceptions.TelegramAPIError:
            logging.exception(f"Failed to send message to user {user_id}")
        except Exception as e:
            logging.exception(f"Something happened: {e}")


def add_new_words_for_user(user_id):
    word_to_add = c.execute(
        '''SELECT id as vocabulary_id, word_az, word_ru, word_emoji, level FROM (SELECT user_id, vocabulary_id FROM user_vocabulary
        WHERE user_id = %s) AS user_words
        RIGHT JOIN vocabulary ON user_words.vocabulary_id = vocabulary.id
        WHERE user_words.user_id IS NULL
        ORDER BY vocabulary.level ASC
        LIMIT 20''',
        (user_id,)
    ).fetchall()
    for word in word_to_add:
        c.execute(
            '''INSERT INTO user_vocabulary (user_id, vocabulary_id, correct_answer_id, num_right_guesses, poll_id)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING''',
            (user_id, word["vocabulary_id"], -1, -1, None)
        )
        conn.commit()
        word['num_right_guesses'] = -1
    return word_to_add


def default_menu(user_id=0) -> types.ReplyKeyboardMarkup:
    menu_keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    menu_keyboard.add("Ещё слово", "Уроки грамматики", "Мой прогресс", "Произношение букв", "Ресурсы по изучению 🇦🇿", "Предложения по боту")
    if user_id in ADMINS_ALL:
        menu_keyboard.add(Keyboard.ADM_STAT.value, Keyboard.ADM_HELP.value)
    return menu_keyboard


def get_unasked_words(words):
    dt = datetime.now()
    words_to_ask = []
    for word in words:
        if word['last_send'] is None:
            words_to_ask.append(word)
        elif dt - word['last_send'] >= timedelta(hours=6):
            words_to_ask.append(word)
    return words_to_ask


def get_asked_words(words):
    dt = datetime.now()
    words_to_ask = []
    for word in words:
        if word['last_send'] is None:
            continue
        elif dt - word['last_send'] < timedelta(hours=6):
            words_to_ask.append(word)
    return words_to_ask


def get_transcription(word):
    transcription = word.get('transcription', '')
    if transcription == '':
        transcription = transcription_inner(word['word_az'])
    return transcription


def transcription_inner(word: str):
    transcription = ''
    is_start = True
    is_end = False
    count = 0
    letter_after = word[1]
    letter_before = ''
    for letter in word:
        transcribe_part = ''
        is_transcribed = False
        letter = letter.lower()
        is_end = (count == len(word) - 1)
        if not is_end:
            letter_after = word[count + 1]
        if not is_start:
            letter_before = word[count - 1]
        if letter in transcribe_az_dict:
            transcription_rule = transcribe_az_dict[letter]
            if is_start & ('start' in transcription_rule):
                transcribe_part = transcription_rule['start']
                is_transcribed = True
            if 'after' in transcription_rule:
                for key in transcription_rule['after']:
                    if letter_before in key:
                        transcribe_part = transcription_rule['after'][key]
                        is_transcribed = True
            if 'before' in transcription_rule:
                for key in transcription_rule['before']:
                    if letter_after in key:
                        transcribe_part = transcription_rule['before'][key]
                        is_transcribed = True
            if not is_transcribed:
                transcribe_part = transcription_rule['regular']
        else:
            transcribe_part = letter
        transcription += transcribe_part
        is_start = False
        count += 1
    return transcription


def check_old_words(internal_user_id):
    if internal_user_id in ADMINS_ALL:
        c.execute(
            '''
            UPDATE user_vocabulary
            SET num_right_guesses = num_right_guesses -2
            WHERE id in (
                SELECT id
                FROM user_vocabulary
                WHERE user_id = %s AND NOW() - last_send > interval '1 month' AND num_right_guesses >= 10
                ORDER BY last_send ASC
                LIMIT 5
            )
            ''',
            (internal_user_id,)
        )
        conn.commit()


if __name__ == "__main__":
    c.execute('SELECT DISTINCT tg_id FROM users WHERE is_blocked = false')
    USER_IDS_TO_SEND_MESSAGES_TO = [row["tg_id"] for row in c.fetchall()]
    logging.info(USER_IDS_TO_SEND_MESSAGES_TO)

    executor.start_polling(dp, skip_updates=False, on_startup=on_startup)
