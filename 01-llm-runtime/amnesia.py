import anthropic

client = anthropic.Anthropic()

def ask(messages):
    r = client.messages.create(model="claude-opus-4-8", max_tokens=100, messages=messages)
    return next(b.text for b in r.content if b.type == "text")

# --- Experiment A: two SEPARATE calls, no shared history ---
print("A1:", ask([{"role": "user", "content": "My name is Luca. Please remember it."}]))
print("A2:", ask([{"role": "user", "content": "What is my name?"}]))

# --- Experiment B: the second question CARRIES the history ---
history = [{"role": "user", "content": "My name is Luca. Please remember it."}]
reply = ask(history)
history.append({"role": "assistant", "content": reply})     # the model's own words, replayed back at it
history.append({"role": "user", "content": "What is my name?"})
print("B:", ask(history))
