# Demo terminal recording script (vhs). Recording the GIF is a manual release step.
Output demo.gif
Set Shell bash
Set FontSize 14
Set Width 1200
Set Height 700

Type "pipx install conveyor-automation"
Enter
Sleep 2s
Type "sudo apt install -y imagemagick"
Enter
Sleep 2s
Type "conveyor example ~/conveyor-demo"
Enter
Sleep 2s
Type "cd ~/conveyor-demo"
Enter
Type "conveyor check png-cleanup.yml"
Enter
Sleep 2s
Type "conveyor run png-cleanup.yml --oneshot"
Enter
Sleep 3s
Type "conveyor serve"
Enter
Sleep 2s
