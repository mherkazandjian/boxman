define AWK_SCRIPT
BEGIN {
    help = "";
}
/^#@help:/ {
    sub(/^#@help:[[:space:]]*/, "", $$0);
    if (help != "") {
        help = help "\n                                 " $$0;
    } else {
        help = $$0;
    }
}
/^#@help:/ {
    sub(/^#@help:[[:space:]]*/, "", $$0);
    help = help $$0;
}
/^#@group:/ {
    # print group line immediately (no target lookup) and preserve color escapes
    sub(/^#@group:[[:space:]]*/, "", $$0);
    grp = $$0;
    gsub(/\\033/, sprintf("%c",27), grp);
    gsub(/\\e/,   sprintf("%c",27), grp);
    printf "%s\n", grp;
    next;
}
/^[a-zA-Z0-9_.@%\/\-]+:/ {
    target = $$0;
    sub(/:.*$$/, "", target);
    # convert common escape sequences (\033 and \e) into a real ESC char so terminal renders colors
    gsub(/\\033/, sprintf("%c",27), help);
    gsub(/\\e/,   sprintf("%c",27), help);
    printf "  %-30s %s\n", target, help;
    help = "";
}
endef
export AWK_SCRIPT
