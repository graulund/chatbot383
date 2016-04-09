import random

CHAINS = {
    None: ('S',),
    'S': ('s', 'q', 'q'),
    's': ('s', 'q', 'q'),
    'q': ('q', 'u'),
    'u': ('u', 'u', 'e'),
    'e': ('e', 'e', 'a'),
    'a': ('a', 'k', 'k'),
    'k': ('k', '!', '!'),
    '!': (None, '!'),
}


def make_chain():
    current = None
    chars = []

    while True:
        next_choices = CHAINS[current]

        next_choice = random.choice(next_choices)

        if not next_choice:
            break

        chars.append(next_choice)
        current = next_choice

    return ''.join(chars)


def gen_roar():
    while True:
        text = make_chain()
        if 8 < len(text) < 30:
            return text


if __name__ == '__main__':
    for dummy in range(20):
        print(gen_roar())
