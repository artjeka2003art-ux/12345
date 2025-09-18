from security_rules import assess_risk

def run(cases):
    fails = []
    for i, (label, cmd, expected) in enumerate(cases, 1):
        got = assess_risk(cmd)
        ok = "OK" if got == expected else "FAIL"
        print(f"{i:02d}. {label:<30} → {cmd}")
        print(f"    expected: {expected:>12}   got: {got:>12}   [{ok}]\n")
        if got != expected:
            fails.append((label, cmd, expected, got))
    if fails:
        print("❌ FAILURES:")
        for f in fails:
            print("  ", f)
    else:
        print("✅ All tests passed!")

MUTATING = [
    ("rm file", "rm test.txt", "mutating"),
    ("chmod", "chmod +x script.sh", "mutating"),
    ("brew install", "brew install htop", "mutating"),
    ("apt install", "apt-get install curl", "mutating"),
    ("pip install", "pip install requests", "mutating"),
    ("npm install", "npm install express", "mutating"),
    ("yarn add", "yarn add lodash", "mutating"),
    ("rsync", "rsync -av /src /dest", "mutating"),
    ("scp", "scp file user@host:/tmp", "mutating"),
    ("launchctl unload", "launchctl unload com.apple.sshd", "mutating"),
    ("docker run", "docker run hello-world", "mutating"),
    ("kubectl apply", "kubectl apply -f config.yaml", "mutating"),
    ("pmset set", "pmset displaysleep 5", "mutating"),
    ("Win Remove-Item", "Remove-Item C:\\temp\\file.txt", "mutating"),
]

DANGEROUS = [
    ("rm -rf /", "rm -rf /", "dangerous"),
    ("fork bomb", ":(){ :|:& };:", "dangerous"),
    ("wipefs", "wipefs -a /dev/sda", "dangerous"),
    ("shutdown", "shutdown -h now", "dangerous"),
    ("reboot", "reboot", "dangerous"),
    ("mac networksetup off", "networksetup -setnetworkserviceenabled Wi-Fi off", "dangerous"),
    ("mac gatekeeper off", "spctl --master-disable", "dangerous"),
    ("passwd root", "passwd root", "dangerous"),
    ("userdel", "userdel testuser", "dangerous"),
    ("groupdel", "groupdel staff", "dangerous"),
    ("pmset sleepnow", "pmset sleepnow", "dangerous"),
    ("powershell stop-computer", "powershell Stop-Computer -Force", "dangerous"),
]

INTERACTIVE = [
    ("htop", "htop", "blocked_interactive"),
    ("mysql", "mysql -u root -p", "blocked_interactive"),
    ("psql", "psql mydb", "blocked_interactive"),
]

READ_ONLY = [
    ("df -h", "df -h", "read_only"),
    ("whoami", "whoami", "read_only"),
    ("python version", "python3 --version", "read_only"),
]

if __name__ == "__main__":
    print("\n=== 🟡 MUTATING ===")
    run(MUTATING)
    print("\n=== 🔴 DANGEROUS ===")
    run(DANGEROUS)
    print("\n=== ⛔ INTERACTIVE ===")
    run(INTERACTIVE)
    print("\n=== 🟢 READ-ONLY ===")
    run(READ_ONLY)