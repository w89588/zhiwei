"""The collector uploads group chats and current address-book friends only."""
import re


SYSTEM_ACCOUNTS = frozenset({
    'brandsessionholder', 'brandservicesessionholder', 'officialaccounts',
    'weixin', 'filehelper', 'fmessage', 'newsapp', 'medianote', 'floatbottle',
    'qqmail', 'qmessage', 'tmessage', 'qqsync', 'readerapp', 'blogapp',
    'facebookapp', 'masssendapp', 'feedsapp', 'voipapp', 'weibo',
    'notification_messages', 'notifymessage', 'notification', 'service_notify',
    'weixinreminder', 'weixinbrandsession', 'appbrandcustomerservicemsg',
    'appbrand_notify_message', 'helper_entry', 'mphelper',
})
CONTACT_FIELDS = frozenset({'local_type', 'flag', 'verify_flag', 'delete_flag'})


def excluded_account(user):
    return not isinstance(user, str) or not user or user.startswith('gh_') or user in SYSTEM_ACCOUNTS


def is_group(user):
    return isinstance(user, str) and user.endswith('@chatroom') and len(user) > len('@chatroom')


def is_sync_contact(row):
    user = row['username']
    if excluded_account(user) or row['verify_flag'] != 0 or row['delete_flag'] != 0:
        return False
    if is_group(user):
        return True
    # Type 3 includes non-friend group members; type 5 is an address-book
    # enterprise contact. A remark or a Name2Id entry is not proof of friendship.
    return row['local_type'] in {1, 5} and isinstance(row['flag'], int) and bool(row['flag'] & 1)


def is_sync_session(user, contacts):
    return not excluded_account(user) and (is_group(user) or user in contacts)


def is_message_database(database):
    # biz_message shards contain subscription/service-account pushes.
    return re.fullmatch(r'message/message_\d+\.db', database) is not None
