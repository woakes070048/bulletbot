# -*- coding: utf-8 -*-

"""
bulletbot.bulletbot
----------------------------------

Defines :class:`.BulletBot`.
"""

import configargparse
import logging
import markovify
import re
import smtplib
import sqlalchemy as sa
import textwrap
import time

from apscheduler.schedulers.blocking import BlockingScheduler
from email.mime.text import MIMEText
from getpass import getpass

from .driver import SQLAlchemyDriver

from .models import (
    Recipient,
    User,
    Bullet,
)


class BulletBot(object):
    """Bot base for reading from and responding to chat servers.

    Example usage::

        bulletBot = BulletBot(sqlalchemy_engine)
        bulletBot.create_bullet('user1', 'Test bullet')
        bulletBot.list_bullets('user1')
        bulletBot.delete_bullets('user1', '1, 4 2')

    """

    logger = logging.getLogger(__name__)

    _email_width = 80
    _default_configs = [
        '~/.bulletbot.ini',
        '/etc/bulletbot.ini',
    ]

    def __init__(self, driver=None, parser=None):
        """To instantiate BulletBot, you need to have pass a sqlalchemy
        database driver that

        1. Has a callable context manager `.session()` that yields a
           SQLAlchemy session

        """

        parser = parser or self.get_parser()
        self.args, _ = parser.parse_known_args()

        if driver:
            self.db = driver
        else:
            self.db = SQLAlchemyDriver(**self.db_settings)

        assert hasattr(self.db, 'session'),\
            'Driver has no session manager'
        assert hasattr(self.db.session, '__call__'),\
            'Driver session manager not callable'

    @property
    def db_settings(self):
        return dict(
            host=self.args.host,
            user=self.args.user,
            password=self.args.password,
            database=self.args.database,
        )

    @staticmethod
    def get_parser():
        parser = configargparse.ArgParser(
            default_config_files=BulletBot._default_configs)
        parser.add('-c', '--config', is_config_file=True, help='config file path')
        parser.add('-H', '--host', env_var='BBOT_HOST', default='localhost')
        parser.add('-d', '--database', env_var='BBOT_DATABASE', default='bullets')
        parser.add('-u', '--user', env_var='BBOT_USER', required=True)
        parser.add('-p', '--password', env_var='BBOT_PASS', required=True)
        parser.add('-t', '--token', env_var='BBOT_SLACK_TOKEN')

        parser.add('--email-user', env_var='BBOT_EMAIL_USER')
        parser.add('--email-from', env_var='BBOT_EMAIL_FROM')
        parser.add('--email-to', env_var='BBOT_EMAIL_TO')
        parser.add('--email-server', env_var='BBOT_EMAIL_SERVER')
        parser.add('--email-pass', env_var='BBOT_EMAIL_PASS')
        parser.add('--email-port', env_var='BBOT_EMAIL_PORT',
                   type=int, default=587)
        parser.add('--cron-hour', env_var='BBOT_CRON_HOUR')
        parser.add('--cron-minute', env_var='BBOT_CRON_MINUTE')

        return parser

    @staticmethod
    def tokenize(text, delim=',[ ]*|[ ]+'):
        """Tokenize a string for with regex deplimeter

        :param text: The string you want to tokenize
        :param delim=',[ ]*|[ ]+': Delimiter to tokenize on
        :returns: :class:`list` of :class:`str` tokens

        Example:

            '1, 2 test' -> ['1', '2', 'test']

        """

        return [index.strip() for index in re.split(delim, text)]

    def markov_nick(self, nick):
        with self.db.session() as s:
            user = s.query(User).filter(User.nick == nick).first()
            if not user:
                return None
            else:
                bullets = [b.bullet for b in user.bullets]

        model = markovify.Text('\n'.join(bullets))
        return model.make_sentence(retries=100)

    def merge_nick(self, nick, realname=None):
        """If no :class:`.models.User` entry with `nick` exists in the
        database, create it.  Since we have a foreign key relationship
        from bullets to users, this should be called before all bullet
        writes.

        :param str nick: The nickname of the user writing the bullet.
        :returns: :class:`str` with channel response

        """

        user = User()
        user.nick = str(nick)
        if realname is not None:
            user.realname = realname

        with self.db.session() as s:
            s.merge(user)

    def register_nick(self, nick, text):
        """Register the pretty name of a user.  This will be the name added to
        the aggregated bullets when sent out at end of day.

        :param str nick: The nickname of the user
        :param str realname: The desired title of the user
        :returns: :class:`str` with channel response

        """

        realname = text
        user = User()
        user.nick = nick
        with self.db.session() as s:
            user = s.merge(user)
            user.realname = realname.strip()

        response = "Registered nick {} as {}".format(nick, realname)
        self.logger.info(response)
        return response

    @staticmethod
    def unsent(s, nick):
        """Query database for a user's unsent bullets (as noted by last_sent
        column)

        :param s: :class:`sqlalchemy.orm.session.Session`
        :param str nick: The nickname of the user
        :returns: :class:`list` of :class:`.models.Bullet`

        """

        return (s.query(Bullet)
                .filter(Bullet.last_sent == None)  # noqa
                .filter(Bullet.nick == nick))

    def create_bullet(self, nick, text):
        """Create a new bullet with the user's nick.

        :param str nick: The nickname of the user
        :param str text: The text of the bullet
        :returns: :class:`str` with channel response

        """

        self.merge_nick(nick)
        bullet = Bullet()
        bullet.bullet = text
        bullet.nick = nick
        with self.db.session() as s:
            s.merge(bullet)

        response = 'Wrote bullet: {}'.format(bullet.bullet)

        self.logger.info((nick, response))
        return response

    def list_bullets(self, nick):
        """List unsent (as noted by last_sent column) bullets with the user's
        nick.

        :param str nick: The nickname of the user
        :returns: :class:`str` with channel response

        """

        with self.db.session() as s:
            bullets = [b.bullet for b in self.unsent(s, nick).all()]

        def get_line(n, bullet):
            return '{n}. {bullet}'.format(n=n, bullet=bullet)

        if bullets:
            lines = [get_line(n, bullet) for n, bullet in enumerate(bullets)]
            response = '\n'.join(lines)
        else:
            response = "No unsent bullets."

        self.logger.info((nick, response))
        return response

    def delete_bullets(self, nick, text):
        """Delete unsent (as noted by last_sent column) bullets with the
        user's nick by index.  The index is an offset pointing to the nth
        unsent bullet with that nick. Given::

            0. bullet A
            1. bullet B
            2. bullet C

        Example text::

            "0, 1 2"

        Will delete 'bullet A' and 'bullet B'.

        :param str nick: The nickname of the user
        :param list nick:
            :class:`list` of :class:`int` indices of bullets to
            delete.
        :returns: :class:`str` with channel response

        """

        try:
            indices = [int(t) for t in self.tokenize(text)]
        except ValueError:
            response = ("Please specify indices of bullets from .list. "
                        "e.g. `.delete 1, 3`")
        else:
            response = self._delete_bullets(nick, indices)

        self.logger.info((nick, response))
        return response

    def _delete_bullets(self, nick, indices):
        """Delete unsent (as noted by last_sent column) bullets

        :param str nick: The nickname of the user
        :param list nick:
            :class:`list` of :class:`int` indices of bullets to
            delete.
        :returns: :class:`str` with channel response

        .. seealso::

            :ref:`.delete_bullets`

        """

        bullets = {}
        for index in indices:
            # Here we get the bullets by the offset on a query for a user's
            # unsent bullets.  This is the view that the user has when
            # listing the bullets.  We want to look up all the bullets
            # first, before we start deleting any.
            with self.db.session() as s:
                bullet = self.unsent(s, nick).offset(index).first()
                if not bullet:
                    return 'Bullet {} not found.'.format(index)
                bullets[index] = bullet
                s.expunge(bullet)

        self.logger.info('Deleting bullets {}'.format(bullets))
        ids = [bullet.id for bullet in bullets.values()]

        with self.db.session() as s:
            # Delete bullets by id, not offset
            count = s.query(Bullet)\
                     .filter(Bullet.id.in_(ids))\
                     .delete(synchronize_session='fetch')

            # Verify that the correct number of bullets were deleted
            assert count == len(ids),\
                'Unable to delete bullets {}'.format(bullets.keys())

        def get_line(n, bullet):
            return "Deleted bullet {}: '{}'".format(n, bullet.bullet)

        lines = [get_line(n, bullet) for n, bullet in bullets.items()]
        response = '\n'.join(lines)

        self.logger.info((nick, response))
        return response

    def create_recipients(self, text):
        """Add a recipient email from user input string.

        Example text::

            "user1@example.com, user2@example.com"

        :param str email: The emails of the user
        :returns: :class:`str` with channel response

        """

        addresses = self.tokenize(text)
        return self._create_recipients(addresses)

    def _create_recipients(self, addresses):
        """Add a recipient email.

        :param list email: List of addresses
        :returns: :class:`str` with channel response

        .. seealso::

            :ref:`.create_recipients`

        """

        with self.db.session() as s:
            for address in addresses:
                recipient = Recipient()
                recipient.email = address
                s.merge(recipient)

        response = "Created recipient addresses '{}'".format(addresses)

        self.logger.info(response)
        return response

    def delete_recipients(self, text):
        """Delete recipient emails from user input string.

        Example text::

            "user1@example.com, user2@example.com"

        :param str email: The emails of the user
        :returns: :class:`str` with channel response

        """

        addresses = self.tokenize(text)
        return self._delete_recipients(addresses)

    def _delete_recipients(self, addresses):
        """Delete a recipient emails.

        :param list email: List of addresses
        :returns: :class:`str` with channel response

        .. seealso::

            :ref:`.delete_recipients`


        """

        with self.db.session() as s:
            for address in addresses:
                s.query(Recipient).filter(Recipient.email == address).delete()

        response = "Created recipient addresses '{}'".format(addresses)

        self.logger.info(response)
        return response

    def get_unsent_bullets(self):
        """Load unset bullets for all users and return a dictionary with `str`
        keys (the realname or nick) and list of :class:`Bullet`
        values.

        """

        bullets = {}
        with self.db.session() as s:
            users = (s.query(User)
                     .join(User.bullets)
                     .filter(Bullet.last_sent == None)  # noqa
                     .all())
            for user in users:
                name = user.realname or user.nick
                bullets[name] = bullets.get(name, [])
                bullets[name] += self.unsent(s, user.nick)

            s.expunge_all()

        return bullets

    def compile_plaintext_bullets(self, unsent_bullets=None):
        """Generate a text paragraph with user bullets

        .. codeblock::

            [User 1]:
              - Logged some bullets


            [user2]:
              - User1's bullet
              - Bullet 2

        """

        if unsent_bullets is None:
            unsent_bullets = self.get_unsent_bullets()

        def format_bullet(bullet):
            prefix = '  - '
            return "{}{}".format(prefix, textwrap.fill(
                bullet.bullet,
                width=self._email_width - len(prefix),
                subsequent_indent=' '*len(prefix)
            ))

        lines = []
        for name, bullets in unsent_bullets.items():
            lines += ['', '[{}]'.format(name)]
            lines += map(format_bullet, bullets)
        response = '\n'.join(lines).strip()

        self.logger.info(response)
        return response

    def mark_all_sent(self):
        """Marks the `last_sent` timestamp on all bullets for which it was
        None.

        This will prevent bullets from being seen in
        :func:`BulletBot.compile_bullets`,
        :func:`BulletBot.list_bullets`, etc.

        """

        with self.db.session() as s:
            return (s.query(Bullet)
                    .filter(Bullet.last_sent == None)  # noqa
                    .update({Bullet.last_sent: sa.func.now()},
                            synchronize_session=False))

    def send_bullets(self, message=None):
        """Sends bullets to recipient per config specification.  If
        :param:`message` is provided, send this message instead.

        """

        if not self.get_unsent_bullets():
            self.logger.warning("No bullets to send")
            return

        msg = MIMEText(message or self.compile_plaintext_bullets())

        assert self.args.email_user, 'No email user specified'
        assert self.args.email_server, 'No email server specified'
        assert self.args.email_to, 'No email recip specified'
        assert self._email_password, 'No email pass specified'

        msg['Subject'] = 'Bullets {}'.format(time.strftime("%m-%d-%Y"))
        msg['From'] = self.args.email_from
        msg['To'] = self.args.email_to

        self.logger.info('Connecting to {}'.format(self.args.email_server))
        server = smtplib.SMTP(self.args.email_server)
        server.starttls()
        server.ehlo()
        server.login(self.args.email_user, self._email_password)

        server.sendmail(
            self.args.email_from,
            [self.args.email_to],
            msg.as_string()
        )

        self.logger.info(msg)

    def send_bullets_mark_sent(self):
        """Send bullets and mark all unsent bullets sent."""

        self.send_bullets()
        self.mark_all_sent()

    def set_email_password(self):
        """Sets the email password from one of two sources

        1. The instances args
        2. If the password is not in the args, prompt a user for it.

        """

        self._email_password = (
            self.args.email_pass or
            getpass('Email password for {}: '.format(self.args.email_user))
        )

    def get_email_cron_args(self):
        """Conditionally pulls the email time scheule arguments into a
        dict.

        """

        return {
            k: v for k, v in dict(
                hour=self.args.cron_hour,
                minute=self.args.cron_minute,
            ).items() if v
        }

    def schedule_send_bullets(self):
        """Blocking call to schedule bullets."""

        self.set_email_password()

        cron_args = self.get_email_cron_args()
        assert cron_args, 'No cron args specified.'

        scheduler = BlockingScheduler()
        scheduler.add_job(self.send_bullets_mark_sent, 'cron', **cron_args)

        self.logger.info("Scheduled for {}".format(cron_args))
        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            raise
