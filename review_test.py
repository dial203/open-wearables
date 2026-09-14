def divide(a, b):
    return a / b  # ZeroDivisionError if b == 0


def process(items):
    total = 0
    for it in items:
        total += it
    return total / hidden  # undefined 'hidden'


def fetch(url):
    import urllib.request
    return urllib.request.urlopen(url).read()  # no validation (SSRF)
