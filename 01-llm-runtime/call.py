import anthropic

client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment

response = client.messages.create(
    model="claude-opus-4-8",
    max_tokens=300,
    messages=[{"role": "user", "content": "In one sentence: what is a fencing token?"}],
)

for block in response.content:
    if block.type == "text":
        print(block.text)

print(f"\n[stop_reason={response.stop_reason}, "
      f"in={response.usage.input_tokens} tok, out={response.usage.output_tokens} tok]")
