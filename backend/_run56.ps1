Set-Location d:\Hackathon\hackathon\backend
$out = python -m pytest tests/unit tests/structure -q -p no:cacheprovider 2>&1 | Out-String
$out | Set-Content -Path d:\Hackathon\hackathon\backend\run56.txt -Encoding utf8
"EXIT=$LASTEXITCODE" | Add-Content -Path d:\Hackathon\hackathon\backend\run56.txt -Encoding utf8
