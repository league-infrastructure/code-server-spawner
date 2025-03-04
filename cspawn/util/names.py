
import random

scientists = [
    "Adleman", "Babbage", "Backus", "Berners-Lee", "Boole", "Brin", "Brooks", "Cerf", "Conway", "Dahl",
    "Diffie", "Dijkstra", "Engelbart", "Erdős", "Fredkin", "Goslin", "Gray", "Hamming", "Hellman", "Hoare",
    "Iverson", "Kahn", "Karp", "Kay", "Knuth", "Lamport", "Liskov", "McCarthy", "Minsky", "Newell",
    "Nygaard", "Page", "Perlis", "Rabin", "Ritchie", "Rivest", "Rossum", "Shamir", "Shannon", "Simon",
    "Stallman", "Stroustrup", "Sutherland", "Swartz", "Tarjan", "Thompson", "Torvalds", "Turing",
    "vonNeumann", "Wilkes", "Wirth", "Zuse"]

adjectives = [
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


def class_code():

    adj = random.choice(adjectives).lower()
    person = random.choice(scientists).lower()
    number = random.randint(10, 99)

    return f"{adj} {person} {number}"
