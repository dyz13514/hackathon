[IO.File]::WriteAllText((Join-Path $PSScriptRoot 'unicode-test.txt'),'智能体生产计划助手',[Text.UTF8Encoding]::new($false))
