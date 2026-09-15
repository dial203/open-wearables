def process(items):
    total = 0
    for it in items:
        total += it
    return total / hidden  # 'hidden' is not defined -> NameError
