
import random

scientists = [
    "Adleman", "Babbage", "Backus", "Berners-Lee", "Boole", "Brin", "Brooks", "Cerf", "Conway", "Dahl",
    "Diffie", "Dijkstra", "Engelbart", "Erdős", "Fredkin", "Goslin", "Gray", "Hamming", "Hellman", "Hoare",
    "Iverson", "Kahn", "Karp", "Kay", "Knuth", "Lamport", "Liskov", "McCarthy", "Minsky", "Newell",
    "Nygaard", "Page", "Perlis", "Rabin", "Ritchie", "Rivest", "Rossum", "Shamir", "Shannon", "Simon",
    "Stallman", "Stroustrup", "Sutherland", "Swartz", "Tarjan", "Thompson", "Torvalds", "Turing",
    "vonNeumann", "Wilkes", "Wirth", "Zuse"]

cs_adj = [
    "imperative", "declarative", "refactored", "sorted", "monotonic",
    "functional", "recursive", "iterative", "parallel", "asynchronous",
    "synchronous", "compiled", "interpreted", "typed", "dynamic",
    "static", "polymorphic", "modular", "scalable", "optimized",
    "encrypted", "hashed", "compressed", "serialized", "deserialized",
    "concurrent", "distributed", "efficient", "deterministic", "nondeterministic",
    "lazy", "eager", "mutable", "immutable", "thread-safe", "event-driven",
    "fault-tolerant", "robust", "lightweight", "heavyweight", "responsive",
    "assembled", "machine-readable", "human-readable", "lexical",
    "syntactic", "semantic", "contextual", "normalized", "indexed",
    "cacheable", "buffered", "streamed", "tokenized", "composable"]

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
    "pears", "oranges", "watermelon", "strawberries", "blueberries", "eggs", "milk",
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


def class_code():
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


if __name__ == "__main__":
    for _ in range(20):
        print(class_code())
