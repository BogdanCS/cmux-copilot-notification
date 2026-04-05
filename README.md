# cmux-copilot-notification

Wrapper around the `copilot` CLI that fires a cmux notification when Copilot is waiting for your input.

## Install cmux (macOS only)

Follow the install instructions at **https://github.com/manaflow-ai/cmux/**

> **Note:** The instructions below are macOS-specific.

## Install the copilot wrapper

After installing cmux, copy the wrapper into the cmux binary directory:

```sh
cp Resources/bin/copilot /Applications/cmux.app/Contents/Resources/bin/copilot
```

The wrapper replaces the `copilot` stub in that directory. When you run
`copilot` inside a cmux terminal, the wrapper spawns the real binary in a PTY
and sends a cmux notification once Copilot goes idle (i.e. it is waiting for
your input). Outside cmux it transparently passes through to the real binary.

## Run the tests

No extra dependencies are required — the test suite uses only the Python
standard library.

```sh
python3 tests/test_copilot_wrapper.py
```
