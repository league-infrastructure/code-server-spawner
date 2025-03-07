
import random

scientists = [
    "Adleman", "Babbage", "Backus", "Berners-Lee", "Boole", "Brin", "Brooks", "Cerf", "Conway", "Dahl",
    "Diffie", "Dijkstra", "Engelbart", "Erdos", "Fredkin", "Goslin", "Gray", "Hamming", "Hellman", "Hoare",
    "Iverson", "Kahn", "Karp", "Kay", "Knuth", "Lamport", "Liskov", "McCarthy", "Minsky", "Newell",
    "Nygaard", "Page", "Perlis", "Rabin", "Ritchie", "Rivest", "Rossum", "Shamir", "Shannon", "Simon",
    "Stallman", "Stroustrup", "Sutherland", "Swartz", "Tarjan", "Thompson", "Torvalds", "Turing",
    "vonNeumann", "Wilkes", "Wirth", "Zuse"]

cs_adj = ['lazy', 'typed', 'eager', 'lossy', 'modal', 'dense', 'paged', 'sorted', 'hashed', 'cached', 'static',
          'robust', 'atomic', 'binary', 'nested', 'linked', 'linear', 'cyclic', 'greedy', 'hybrid', 'finite',
          'native', 'latent', 'sparse', 'opaque', 'signed', 'public', 'secure', 'faulty', 'masked', 'indexed',
          'mutable', 'lexical', 'dynamic', 'modular', 'virtual', 'bounded', 'generic', 'logical', 'visible',
          'literal', 'private', 'blocked', 'aligned', 'patched', 'buffered', 'streamed', 'compiled', 'parallel',
          'scalable', 'semantic', 'volatile', 'lossless', 'directed', 'weighted', 'abstract', 'portable',
          'unsigned', 'implicit', 'explicit', 'balanced', 'tokenized', 'optimized', 'encrypted', 'efficient',
          'immutable', 'assembled', 'syntactic', 'recursive', 'iterative', 'heuristic', 'protected',
          'ephemeral', 'redundant', 'sandboxed', 'quantized', 'refactored', 'compressed', 'serialized',
          'concurrent', 'responsive', 'contextual', 'normalized', 'composable', 'imperative', 'functional',
          'linearized', 'persistent', 'serialized', 'vectorized', 'distributed', 'thread-safe']

short_adjectives = [
    "big", "small", "fast", "slow", "happy", "sad", "funny", "serious", "loud", "quiet",
    "bright", "dark", "clean", "dirty", "strong", "weak", "brave", "scared", "smart", "silly",
    "kind", "mean", "rich", "poor", "tall"
]

animals_plural = [
    "cats", "dogs", "bats", "rats", "cows", "pigs", "mice", "deer", "fish",
    "frogs", "goats", "birds", "bears", "ducks", "foxes", "wolves", "sheep",
    "geese", "horses", "rabbits", "turtles", "snakes", "eagles", "owls", "lions"
]

colors = [
    "red", "blue", "green", "yellow", "purple", "orange", "pink", "brown", "black", "white",
    "gray", "cyan", "magenta", "lime", "indigo", "violet", "gold", "silver", "bronze", "teal",
    "navy", "maroon", "olive", "peach", "turquoise"
]


foods = [
    "apples", "bananas", "carrots", "bread", "cheese", "chicken", "pizza", "pasta",
    "rice", "beans", "tomatoes", "onions", "potatoes", "lettuce", "grapes", "peaches",
    "pears", "oranges", "melon", "berries", "eggs", "milk",
    "yogurt", "butter"
]

# Verbs for plural animal names (e.g., "Dogs <verb> pizza.")
plural_verbs = [
    "eat", "like", "love", "prefer", "chew", "devour", "enjoy", "bite", "taste",
    "nibble", "gulp", "gobble", "consume", "chomp", "crave"
]

# Verbs for singular animal names (e.g., "A dog <verb> pizza.")
singular_verbs = [
    "eats", "likes", "loves", "prefers", "admires", "smells", "enjoys", "pets", "tastes",
    "nibbles", "gulps", "gobbles", "consumes", "chomps", "craves"
]


def _class_code():
    """Generate a random class code. 

    There are only about 1M codes, so there isn't a lot of entropy here. """

    match random.randint(1, 5):
        case 1:
            person = random.choice(scientists).lower()
            verb = random.choice(singular_verbs)
            food = random.choice(foods)
            code = f"{person} {verb} {food}"
        case 2:
            person = random.choice(scientists).lower()
            verb = random.choice(singular_verbs)
            animal = random.choice(animals_plural)
            code = f"{person} {verb} {animal}"
        case 3:
            num = random.randint(1, 99)
            animal = random.choice(animals_plural)
            verb = random.choice(plural_verbs)
            food = random.choice(foods)
            code = f"{num} {animal} {verb} {food}"
        case 4:
            person = random.choice(scientists).lower()
            adj = random.choice(cs_adj)
            code = f"{adj} {person}"
        case 5:
            num = random.randint(1, 99)
            animal = random.choice(animals_plural)
            adj = random.choice(cs_adj)
            code = f"{num} {adj} {animal}"
        case 6:
            num = random.randint(1, 99)
            animal = random.choice(animals_plural)
            adj = random.choice(short_adjectives)
            code = f"{num} {adj} {animal}"

    return code


def class_code():
    """ Generata a class code, but try to find a short one. """

    while (cc := _class_code()) and len(cc) > 25:
        pass

    return cc


if __name__ == "__main__":
    for _ in range(20):
        print(class_code())
