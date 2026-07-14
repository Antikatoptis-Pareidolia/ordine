# Demo terminal recording script (vhs). Recording the GIF is a manual release step.
Output demo/demo.gif
Output demo/demo.mp4
Output demo/demo.mp4
Set Shell bash
Set FontSize 16
Set Width 1200
Set Height 700

Hide
Type "rm -rf /tmp/ordine-demo /tmp/ordine-demo-data && export XDG_DATA_HOME=/tmp/ordine-demo-data XDG_CONFIG_HOME=/tmp/ordine-demo-data && clear"
Enter
Show

Type "ordine example /tmp/ordine-demo"
Enter
Sleep 3s
Type "cd /tmp/ordine-demo"
Enter
Sleep 1s
Type "ordine run png-cleanup.yml --oneshot"
Enter
Sleep 4s
Type "ls exports/"
Enter
Sleep 4s
