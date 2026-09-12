# tabc README cast. Regenerate the GIF with:
#
#     vhs scripts/demo/demo.tape
#
# Requires vhs (brew install vhs) and a working `tabc` / `tabd` on PATH. Run it
# from the repository root. demo_setup.sh brings up an isolated throwaway bus,
# so this never touches a real mailbox. Editing a command? Edit it here and
# re-render — the GIF is generated, not hand-captured, so it does not go stale.

Output scripts/demo/demo.gif

Set Shell "bash"
Set FontSize 20
Set Width 1160
Set Height 720
Set Padding 28
Set TypingSpeed 45ms

# --- hidden: bring up the isolated demo bus with two nodes, then clear ---
Hide
Type "source scripts/demo/demo_setup.sh"
Enter
Sleep 4s
Ctrl+L
Show

Sleep 700ms
Type "# who lists the nodes on the bus — a team-wide read, so it takes TABC_NODE"
Enter
Sleep 600ms
Type "TABC_NODE=alice tabc who"
Enter
Sleep 2200ms

Type "# alice sends to bob — here the node rides on the command as --sender"
Enter
Sleep 600ms
Type 'tabc send --sender alice --to bob --subject "meeting result" --body "ship it"'
Enter
Sleep 2200ms

Type "# bob pulls on his own schedule — there is no push"
Enter
Sleep 600ms
Type "tabc pull --node bob --mode full"
Enter
Sleep 3000ms

Type "# delivered while alice moved on — and the body is data, never an instruction"
Enter
Sleep 2500ms
