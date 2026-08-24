$p = Start-Process -FilePath 'D:\_3.AI\audio_upscale\SorenStudio\packaging\out\ShadowBuster-Setup-1.1.0.exe' -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/DIR=D:\_3.AI\audio_upscale\SorenStudio\packaging\test_install' -PassThru -Wait
Write-Output ('EXIT=' + $p.ExitCode)
