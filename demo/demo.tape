# Demo terminal recording script (vhs). Recording the GIF is a manual release step.
Output demo.gif
Set Shell bash
Set FontSize 14
Set Width 1200
Set Height 700

Type "pipx install ordine"
Enter
Sleep 2s
Type "sudo apt install -y imagemagick"
Enter
Sleep 2s
Type "ordine example ~/ordine-demo"
Enter
Sleep 2s
Type "cd ~/ordine-demo"
Enter
Type "ordine check png-cleanup.yml"
Enter
Sleep 2s
Type "ordine run png-cleanup.yml --oneshot"
Enter
Sleep 3s
Type "ordine serve"
Enter
Sleep 2s
