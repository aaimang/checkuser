# Demo GIF for usergraph.py. Uses fake data from make_demo_data.py, not data/.
# Make it again from the repo root with:  vhs docs/demo/usergraph.tape

Output docs/demo/usergraph.gif

Set Shell "bash"
Set FontSize 14
Set Width 1200
Set Height 680
Set Padding 16
Set Theme "Catppuccin Mocha"
Set TypingSpeed 40ms

Hide
Type "export PS1='$ ' DEMO=$(mktemp -d)/snapshots.jsonl && set +m" Enter
Type "python3 docs/demo/make_demo_data.py $DEMO" Enter
# Warm the uv cache so the GIF does not show a package install.
Type "uv run --script usergraph.py --file $DEMO --tz UTC --range day --once > /dev/null" Enter
Sleep 4s
# Add a jump in users 14 s after start, to show the live redraw.
Type "(sleep 14; python3 docs/demo/make_demo_data.py $DEMO --append) &" Enter
Type "clear" Enter
Show

Type "uv run --script usergraph.py --file $DEMO" Sleep 500ms Enter
Sleep 2s
Type "3" Sleep 600ms Enter
Sleep 1.5s
Type "3" Sleep 600ms Enter
Sleep 13s
Ctrl+C
Sleep 1s
