import collections
import copy
import json
import logging
import os
import random
import re
import sqlite3
import time
import datetime
import dateutil.relativedelta

import arrow

from chatbot383.bot import Limiter
from chatbot383.featurecomponents.matchgen import MatchGenerator, MatchError
from chatbot383.featurecomponents.tellnextdb import TellnextGenerator
from chatbot383.featurecomponents.tokennotify import TokenNotifier
from chatbot383.regex import RegexServer, RegexTimeout
from chatbot383.roar import gen_roar


_logger = logging.getLogger(__name__)
_random = random.Random()


class MailbagFullError(ValueError):
    pass


class SenderOutboxFullError(MailbagFullError):
    pass


class Database(object):
    def __init__(self, db_path):
        self._path = db_path
        self._con = sqlite3.connect(db_path)

        self._init_db()

    def _init_db(self):
        with self._con:
            self._con.execute('''PRAGMA journal_mode=WAL;''')
            self._con.execute('''CREATE TABLE IF NOT EXISTS mail
            (id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER NOT NULL,
            username TEXT NOT NULL,
            text TEXT NOT NULL,
            status TEXT NOT NULL
            )
            ''')
            self._con.execute('''CREATE INDEX IF NOT EXISTS mail_status_index
            ON mail (status)
            ''')

    def get_mail(self, skip_username=None):
        with self._con:
            row = self._con.execute(
                '''SELECT id, username, text, timestamp FROM
                mail WHERE status = ? AND username != ? LIMIT 1''',
                ('unread', skip_username or '')).fetchone()

            if row:
                mail_info = {
                    'username': row[1],
                    'text': row[2],
                    'timestamp': row[3],
                }
                self._con.execute('''UPDATE mail SET status = ?
                WHERE id = ?''', ('read', row[0]))
                return mail_info

    def get_old_mail(self):
        with self._con:
            row = self._con.execute('''SELECT max(id) FROM mail''').fetchone()

            max_id = row[0]

            for dummy in range(10):
                # Retry a few times until we get an old one
                row = self._con.execute(
                    '''SELECT username, text, timestamp FROM
                    mail WHERE status = ? AND id > ?''',
                    ('read', _random.randint(0, max_id))
                ).fetchone()

                if row:
                    mail_info = {
                        'username': row[0],
                        'text': row[1],
                        'timestamp': row[2],
                    }
                    return mail_info

    def put_mail(self, username, text):
        with self._con:
            row = self._con.execute(
                '''SELECT count(1) FROM mail
                WHERE status = 'unread' AND username = ? LIMIT 1
                ''',
                (username,)
            ).fetchone()

            if row[0] >= 10:
                raise SenderOutboxFullError()

            row = self._con.execute('''SELECT count(1) FROM mail
            WHERE status = 'unread' LIMIT 1''').fetchone()

            if row[0] >= 100:
                raise MailbagFullError()

            self._con.execute('''INSERT INTO mail
            (timestamp, username, text, status) VALUES (?, ?, ?, 'unread')
            ''', (int(time.time()), username, text))

    def get_status_count(self, status):
        with self._con:
            row = self._con.execute('''SELECT count(1) FROM mail
            WHERE status = ? LIMIT 1''', (status,)).fetchone()

            return row[0]


class Features(object):
    DONGER_SONG_TEMPLATE = (
        'I like to raise my {donger} I do it all the time ヽ༼ຈل͜ຈ༽ﾉ '
        'and every time its lowered┌༼ຈل͜ຈ༽┐ '
        'I cry and start to whine ┌༼@ل͜@༽┐'
        'But never need to worry ༼ ºل͟º༽ '
        'my {donger}\'s staying strong ヽ༼ຈل͜ຈ༽ﾉ'
        'A {donger} saved is a {donger} earned so sing the {donger} song!'
    )
    TOO_LONG_TEXT_TEMPLATE = '{} Message length exceeds my capabilities!'
    MAIL_MAX_LEN = 300
    FOOD_LOG_FILENAME = 'foodlog.txt'

    def __init__(self, bot, help_text, database, config, alert_channels=None):
        self._bot = bot
        self._help_text = help_text
        self._database = database
        self._config = config
        self._recent_messages_for_regex = collections.defaultdict(lambda: collections.deque(maxlen=100))
        self._last_message = {}
        self._spam_limiter = Limiter(min_interval=10)
        self._regex_server = RegexServer()
        #self._token_notifier = TokenNotifier(
        #    config.get('token_notify_filename'),
        #    config.get('token_notify_channels'),
        #    config.get('token_notify_interval', 60)
        #)
        self._tellnext_generator = None
        self._alert_channels = frozenset(alert_channels or ())

        self._food_current = ""
        self._food_next = ""

        self._food_current_updated = None
        self._food_next_updated = None

        self._mail_disabled_channels = config.get('mail_disabled_channels')
        #    self._tellnext_generator = TellnextGenerator(config['tellnext_database'])

        bot.register_message_handler('pubmsg', self._collect_recent_message)
        bot.register_message_handler('action', self._collect_recent_message)
        #bot.register_command(r's/(.+/.*)', self._regex_command)
        #bot.register_command(r'(?i)!double(team)?($|\s.*)', self._double_command)
        bot.register_command(r'(?i)!(groudonger)?help($|\s.*)', self._help_command)
        bot.register_command(r'(?i)!groudon(ger)?($|\s.*)', self._roar_command)
        #bot.register_command(r'(?i)!hypestats($|\s.*)', self._hype_stats_command)
        bot.register_command(r'(?i)!klappa($|\s.*)', self._klappa_command)
        #bot.register_command(r'(?i)!(mail|post)($|\s.*)$', self._mail_command)
        #bot.register_command(r'(?i)!(mail|post)status($|\s.*)', self._mail_status_command)
        #bot.register_command(r'(?i)!pick\s+(.*)', self._pick_command)
        #bot.register_command(r'(?i)!praise($|\s.{,100})$', self._praise_command)
        #bot.register_command(r'(?i)!(?:shuffle|scramble)($|\s.*)', self._shuffle_command)
        #bot.register_command(r'(?i)!song($|\s.{,50})$', self._song_command)
        #bot.register_command(r'(?i)!sort($|\s.*)', self._sort_command)
        #bot.register_command(r'(?i)!rand(?:om)?case($|\s.*)', self._rand_case_command)
        #bot.register_command(r'(?i)!release($|\s.{,100})$', self._release_command)
        #bot.register_command(r'(?i)!riot($|\s.{,100})$', self._riot_command)
        #bot.register_command(r'(?i)!rip($|\s.{,100})$', self._rip_command)
        #bot.register_command(r'(?i)!gen(?:erate)?match($|\s.*)$', self._generate_match_command)
        #bot.register_command(r'(?i)!(xd|minglee|chfoo)($|\s.*)', self._xd_command)
        # Temporary disabled. interferes with rate limit
        # bot.register_command(r'.*\b[xX][dD] +MingLee\b.*', self._xd_rand_command)
        #bot.register_command(r'(?i)!(wow)($|\s.*)', self._wow_command)
        bot.register_command(r'(?i)!foodcurrent($|\s.*)', self._food_current_command)
        bot.register_command(r'(?i)!foodnext($|\s.*)', self._food_next_command)

        self._reseed_rng_sched()
        #self._token_notify_sched()

    def _reseed_rng_sched(self):
        _reseed()
        _logger.debug('RNG reseeded')
        self._bot.scheduler.enter(300, 0, self._reseed_rng_sched)

    def _token_notify_sched(self):
        interval = self._token_notifier.notify(self._bot)

        if not interval:
            interval = 60

        _logger.debug('Next token analysis interval %s', interval)

        self._bot.scheduler.enter(interval, 0, self._token_notify_sched)

    @classmethod
    def is_too_long(cls, text):
        return len(text.encode('utf-8', 'replace')) > 400

    @classmethod
    def _try_say_or_reply_too_long(cls, formatted_text, session):
        if cls.is_too_long(formatted_text):
            session.reply(cls.TOO_LONG_TEXT_TEMPLATE.format(gen_roar()))
            return False
        else:
            session.say(formatted_text)
            return True

    def _collect_recent_message(self, session):
        if session.message['event_type'] in ('pubmsg', 'action'):
            channel = session.message['channel']
            username = session.message['username']
            our_username = session.client.get_nickname(lower=True)

            if username != our_username:
                #self._recent_messages_for_regex[channel].append(session.message)

                #if not session.message['text'].startswith('!'):
                #    self._last_message[channel] = session.message
                if username.lower() == "food" and channel.lower() == "#food":
                    self._collect_food_message(session.message)

    def _help_command(self, session):
        session.reply('{} {}'.format(gen_roar(), self._help_text))

    def _roar_command(self, session):
        session.say('{} {} {}'.format(gen_roar(), gen_roar(), gen_roar().upper()))

    def _hype_stats_command(self, session):
        stats_filename = self._config.get('hype_stats_filename')

        if not stats_filename or \
                stats_filename and not os.path.exists(stats_filename):
            session.reply(
                '{} This command is currently unavailable!'.format(gen_roar()))
            return

        try:
            with open(stats_filename) as file:
                doc = json.load(file)

            text_1 = '[{duration}] Lines/sec {averages_str} ' \
                '· Hints/sec {hint_averages_str}'.format(
                    duration=doc['stats']['duration'],
                    averages_str=doc['stats']['averages_str'],
                    hint_averages_str=doc['stats']['hint_averages_str'],
            )
            text_2 = 'Chat {chat_graph} · Hint {hint_graph}'.format(
                chat_graph=doc['stats']['chat_graph'],
                hint_graph=doc['stats']['hint_graph'],
            )
        except (ValueError, KeyError, IndexError):
            _logger.exception('Error formatting stats')
        else:
            session.say(text_1)
            session.say(text_2)

    def _regex_command(self, session):
        # Special split http://stackoverflow.com/a/21107911/1524507
        parts = re.split(r'(?<!\\)/', session.match.group(1))

        if not (2 <= len(parts) <= 3):
            return

        search_pattern = parts[0]
        replacement = parts[1]
        options = parts[2] if len(parts) == 3 else ''
        flags = 0
        count = 1

        if 'i' in options:
            flags |= re.IGNORECASE

        if 'g' in options:
            count = 0

        try:
            pattern = re.compile(search_pattern, flags)
        except re.error as error:
            session.reply('{} {}!'.format(gen_roar(), error.args[0].title()))
            return

        for history_message in reversed(self._recent_messages_for_regex[session.message['channel']]):
            text = history_message['text']
            channel = session.message['channel']

            if text.startswith('s/'):
                continue

            try:
                matched = self._regex_server.search(pattern, text)
            except RegexTimeout:
                _logger.warning(
                    'Regex DoS by %s on %s', session.message['username'],
                    session.message['channel'])
                session.reply(gen_roar().upper())
                return

            if matched:
                try:
                    new_text = pattern.sub(replacement, text, count=count)
                except re.error as error:
                    session.reply('{} {}!'.format(gen_roar(), error.args[0].title()))
                    return

                if _random.random() < 0.1:
                    new_text = gen_roar()
                    fake_out = True
                else:
                    fake_out = False

                formatted_text = '{user} wishes to {stacked}correct ' \
                    '{target_user}: {text}'.format(
                        user=session.message['nick'],
                        target_user=history_message['nick'],
                        text=new_text,
                        stacked='re' if history_message.get('stacked') else '',
                )

                ok = self._try_say_or_reply_too_long(formatted_text, session)
                if not ok:
                    return

                if not fake_out:
                    stacked_message = copy.copy(history_message)
                    stacked_message['text'] = new_text
                    stacked_message['stacked'] = True
                    self._recent_messages_for_regex[channel].append(stacked_message)

                return

        session.reply('{} Your request does not apply to any recent messages!'
                      .format(gen_roar()))

    def _double_command(self, session):
        text = session.match.group(2).strip()
        last_message = self._last_message.get(session.message['channel'])

        if not text and (session.match.group(1) or not last_message):
            text = 'ヽ༼ຈل͜ຈ༽ﾉ DOUBLE TEAM ヽ༼ຈل͜ຈ༽ﾉ'
        elif not text:
            text = last_message['text']

        double_text = ''.join(char * 2 for char in text)
        formatted_text = '{} Doubled! {}'.format(gen_roar(), double_text)

        self._try_say_or_reply_too_long(formatted_text, session)

    def _pick_command(self, session):
        text = session.match.group(1).strip()

        if not text:
            text = 'heads,tails'

        result = _random.choice(text.split(',')).strip()
        formatted_text = '{} Picked! {}'.format(gen_roar(), result)

        self._try_say_or_reply_too_long(formatted_text, session)

    def _praise_command(self, session):
        text = session.match.group(1).strip()

        if text:
            formatted_text = '{} Praise {}!'.format(gen_roar(), text)
        else:
            formatted_text = '{} Praise it! Raise it!'.format(gen_roar())

        self._try_say_or_reply_too_long(formatted_text, session)

    def _shuffle_command(self, session):
        text = session.match.group(1).strip()
        last_message = self._last_message.get(session.message['channel'])

        if not text and last_message:
            text = last_message['text']
        elif not text:
            text = 'Groudonger'

        shuffle_list = list(text)
        _random.shuffle(shuffle_list)
        shuffle_text = ''.join(shuffle_list).strip()

        formatted_text = '{} Shuffled! {}'.format(gen_roar(), shuffle_text)

        self._try_say_or_reply_too_long(formatted_text, session)

    def _song_command(self, session):
        limiter_key = ('song', session.message['channel'])
        if not self._spam_limiter.is_ok(limiter_key):
            return

        text = session.match.group(1).strip()

        if not text:
            text = 'Groudonger'

        formatted_text = self.DONGER_SONG_TEMPLATE.format(donger=text)

        self._try_say_or_reply_too_long(formatted_text, session)

        self._spam_limiter.update(limiter_key)

    def _sort_command(self, session):
        text = session.match.group(1).strip()
        last_message = self._last_message.get(session.message['channel'])

        if not text and last_message:
            text = last_message['text']
        elif not text:
            text = 'Groudonger'

        sorted_text = ''.join(sorted(text)).strip()
        formatted_text = '{} Sorted! {}'.format(gen_roar(), sorted_text)

        self._try_say_or_reply_too_long(formatted_text, session)

    def _rand_case_command(self, session):
        text = session.match.group(1).strip()
        last_message = self._last_message.get(session.message['channel'])

        if not text and last_message:
            text = last_message['text']
        elif not text:
            text = 'Groudonger'

        rand_case_text = ''.join(
            char.swapcase() if _random.randint(0, 1) else char for char in text
        )

        formatted_text = '{} Random case! {}'.format(gen_roar(), rand_case_text)

        self._try_say_or_reply_too_long(formatted_text, session)

    def _release_command(self, session):
        text = session.match.group(1).strip() or session.message['nick']

        formatted_text = \
            '{roar} {text} was released. Farewell, {text}!'.format(
                roar=gen_roar(), text=text
            )

        self._try_say_or_reply_too_long(formatted_text, session)

    def _riot_command(self, session):
        text = session.match.group(1).strip()

        if text:
            formatted_text = '{} {} or riot! {}'\
                .format(gen_roar(), text, gen_roar().upper())
        else:
            formatted_text = \
                '{} {} {}'.format(
                    gen_roar(),
                    _random.choice((
                        'Riot, I say! Riot, you may!',
                        'Riot!',
                        '{} riot!'.format(session.message['nick']),
                        'Groudonger riot!',
                    )),
                    gen_roar().upper()
                )

        self._try_say_or_reply_too_long(formatted_text, session)

    def _rip_command(self, session):
        text = session.match.group(1).strip() or session.message['nick']

        formatted_text = \
            '{} {}, {}. Press F to pay your respects.'.format(
                gen_roar(), _random.choice(('RIP', 'Rest in peace')), text
            )

        self._try_say_or_reply_too_long(formatted_text, session)

    def _klappa_command(self, session):
        session.say('{}'.format(_random.choice(('Kappa //', gen_roar()))))

    def _xd_command(self, session):
        session.say('{} xD MingLee'.format(
            gen_roar().lower().replace('!', '?'))
        )

    def _xd_rand_command(self, session):
        if _random.random() < 0.1 or \
                session.message['username'] == 'wow_deku_onehand' and \
                session.message['text'].strip() == 'xD MingLee':
            def rep_func(match):
                return '!' if _random.random() < 0.6 else '1'

            session.say('{} xD MingLee'.format(
                re.sub('!', rep_func, gen_roar().lower()))
            )

    def _wow_command(self, session):
        if self._tellnext_generator:
            session.say('> {}'.format(self._tellnext_generator.get_paragraph()))
        else:
            session.reply('{} Feature not available!'.format(gen_roar()))

    def _mail_command(self, session):
        if session.message['channel'] in self._mail_disabled_channels:
            session.reply(
                '{} My mail services cannot be used here.'
                .format(gen_roar().replace('!', '.'))
            )
            return

        mail_text = session.match.group(2).strip()

        if mail_text:
            if len(mail_text) > self.MAIL_MAX_LEN:
                session.reply(
                    '{} Your message is too burdensome! '
                    'Send a concise version instead. '
                    '({}/{})'
                    .format(gen_roar(), len(mail_text), self.MAIL_MAX_LEN)
                )
                return

            try:
                self._database.put_mail(session.message['username'], mail_text)
            except SenderOutboxFullError:
                session.reply(
                    '{} How embarrassing! Your outbox is full!'
                    .format(gen_roar()))
            except MailbagFullError:
                session.reply(
                    '{} Incredulous! My mailbag is full! Read one instead!'
                    .format(gen_roar()))
            else:
                session.reply(
                    'Tremendous! I will deliver this mail to the next '
                    'recipient without fail! {}'.format(gen_roar()))
        else:
            if _random.random() < 0.3:
                mail_info = self._database.get_old_mail()
            else:
                if _random.random() < 0.7:
                    skip_username = session.message['username']
                else:
                    skip_username = None

                mail_info = self._database.get_mail(skip_username=skip_username)

                if not mail_info and _random.random() < 0.3:
                    mail_info = self._database.get_old_mail()

            if not mail_info:
                session.reply(
                    '{} Outlandish! There is no new mail! You should send some!'
                    .format(gen_roar())
                )
            else:
                session.reply(
                    '{roar} I am delivering mail! '
                    'Here it is, {date}, from {username}: {msg}'
                    .format(
                        roar=gen_roar(),
                        username=mail_info['username'].title(),
                        date=arrow.get(mail_info['timestamp']).humanize(),
                        msg=mail_info['text']),
                    multiline=True
                )

    def _mail_status_command(self, session):
        unread_count = self._database.get_status_count('unread')
        read_count = self._database.get_status_count('read')

        session.reply(
            '{roar} {unread} unread, {read} read, {total} total!'.format(
                roar=gen_roar(),
                unread=unread_count,
                read=read_count,
                total=unread_count + read_count
            )
        )

    def _alert_important_food_change(self, title, isNow):
        # This is only called if the title is different from what we already had in our variables
        if re.search(r"\bjulia\b", title, re.I) or re.search(r"\bfrench\s+chef\b", title, re.I):
            if isNow:
                out = 'twitch.tv/food is now playing "{}"'.format(title)
            else:
                out = 'twitch.tv/food will play "{}" next'.format(title)

            for channel in self._alert_channels:
                self._bot.send_text(channel, 'PogChamp {}'.format(out))

    def _log_food_nowplaying(self):
        output = "{}: {}\n".format(self._food_current_updated.isoformat(), self._food_current)
        with open(self.FOOD_LOG_FILENAME, "a") as logfile:
            logfile.write(output)

    def _collect_food_message(self, message):
        text = message['text']

        # What's next
        whats_next = ""
        match = re.fullmatch(r"\s*will play \"([^\"]+)\" next\s*", text, re.I)

        if match and self._food_next != "voting":
            # This particular format is invalid when voting
            whats_next = match.group(1)

        match = re.fullmatch(r"\s*the vote is done! the winner is \"([^\"]+)\" with [0-9]+ votes?\s*", text, re.I)

        if match:
            whats_next = match.group(1)

        if whats_next and self._food_next != whats_next:
            self._alert_important_food_change(whats_next, False)
            self._food_next = whats_next
            self._food_next_updated = datetime.datetime.now(datetime.timezone.utc)

        # What's playing
        match = re.fullmatch(r"\s*now playing \"([^\"]+)\"\s*", text, re.I)

        if match:
            whats_on = match.group(1)
            if whats_on and self._food_current != whats_on:
                self._alert_important_food_change(whats_on, True)
                self._food_current = whats_on
                self._food_current_updated = datetime.datetime.now(datetime.timezone.utc)
                self._log_food_nowplaying()

                # If this was previously "next", then now we don't know what "next" is.
                if self._food_next == self._food_current:
                    self._food_next = ""
                    self._food_next_updated = datetime.datetime.now(datetime.timezone.utc)

        # Voting in progress
        if text == "TIME TO VOTE!":
            whats_next = "voting"
            if self._food_next != whats_next:
                self._food_next = whats_next
                self._food_next_updated = datetime.datetime.now(datetime.timezone.utc)

    def _food_updated_string(self, dt):
        if not dt:
            return ""

        now = datetime.datetime.now(datetime.timezone.utc)
        rd = dateutil.relativedelta.relativedelta(now, dt)

        attrs = ['years', 'months', 'days', 'hours', 'minutes', 'seconds']
        human_readable = lambda delta: [
            '%d %s' % (getattr(delta, attr), getattr(delta, attr) > 1 and attr or attr[:-1])
            for attr in attrs if getattr(delta, attr)
        ]

        hr = human_readable(rd)
        if len(hr) > 0:
            return "(last updated {} ago)".format(hr[0])
        return ""

    def _food_current_command(self, session):
        updated_string = self._food_updated_string(self._food_current_updated)
        if not self._food_current:
            session.reply(
                '{} I have no idea what\'s playing on twitch.tv/food right now! :( {}'
                .format(gen_roar(), updated_string)
            )
        else:
            session.reply(
                '{roar} twitch.tv/food is now playing "{title}"! {updated}'
                .format(
                    roar=gen_roar(),
                    title=self._food_current,
                    updated=updated_string
                )
            )

    def _food_next_command(self, session):
        updated_string = self._food_updated_string(self._food_next_updated)
        if not self._food_next:
            session.reply(
                '{} I have no idea what\'s playing on twitch.tv/food next! :( {}'
                .format(gen_roar(), updated_string)
            )
        else:
            title = self._food_next
            if title == "voting":
                message = "twitch.tv/food chat is currently voting for what to play next."
            else:
                message = 'twitch.tv/food will play "{}" next!'.format(title)
            session.reply('{} {} {}'.format(gen_roar(), message, updated_string))


_seed = int.from_bytes(os.urandom(2500), 'big')  # copied from std lib


def _reseed():
    # scrubs keep complaining about the rng so this function exists
    global _seed
    _seed = _seed ^ int.from_bytes(os.urandom(32), 'big')
    _random.seed(_seed)

    for dummy in range(1000):
        # Discard the first few so people can't say it's biased initially
        _random.random()
