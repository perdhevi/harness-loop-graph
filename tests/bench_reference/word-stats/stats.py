import sys

text = open(sys.argv[1]).read()
print(f'{len(text.split())} words, {len(text.splitlines())} lines')
