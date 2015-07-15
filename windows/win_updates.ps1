#!powershell
# This file is part of Ansible
#
# Copyright 2015, Matt Davis <mdavis@ansible.com>
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.

# WANT_JSON
# POWERSHELL_COMMON

$ErrorActionPreference = "Stop"

$job_body = 
{
    Param(
    [hashtable]$boundparms=@{},
    [Object[]]$unboundargs=$()
    )

    $ErrorActionPreference = "Stop"
    $DebugPreference = "Continue"
    
    if(-not $(Test-Path variable:log_path)) { $log_path = $null }

    Set-StrictMode -Version 2

    Function Write-DebugLog
    {
        Param(
        [string]$msg
        )

        $DebugPreference = "Continue"
        $date_str = Get-Date -Format u
        $msg = "$date_str $msg"
        Write-Debug $msg

        if($log_path -ne $null)
        {
            Add-Content $log_path $msg
        }
    }

    # TODO: elevate this to module arg validation once we have it
    Function MapCategoryNameToGuid
    {
        Param([string] $CategoryName)

        $CategoryGUID = switch -exact ($CategoryName)
        {
            # as documented by TechNet @ https://technet.microsoft.com/en-us/library/ff730937.aspx
            "Application" {"5C9376AB-8CE6-464A-B136-22113DD69801"}
            "Connectors" {"434DE588-ED14-48F5-8EED-A15E09A991F6"}
            "CriticalUpdates" {"E6CF1350-C01B-414D-A61F-263D14D133B4"}
            "DefinitionUpdates" {"E0789628-CE08-4437-BE74-2495B842F43B"}
            "DeveloperKits" {"E140075D-8433-45C3-AD87-E72345B36078"}
            "FeaturePacks" {"B54E7D24-7ADD-428F-8B75-90A396FA584F"}
            "Guidance" {"9511D615-35B2-47BB-927F-F73D8E9260BB"}
            "SecurityUpdates" {"0FA1201D-4330-4FA8-8AE9-B877473B6441"}
            "ServicePacks" {"68C5B0A3-D1A6-4553-AE49-01D3A7827828"}
            "Tools" {"B4832BD8-E735-4761-8DAF-37F882276DAB"}
            "UpdateRollups" {"28BC880E-0592-4CBF-8F95-C79B17911D5F"}
            "Updates" {"CD5FFD1E-E932-4E3A-BF74-18BF0B1BBD83"}
            default { throw "Unknown CategoryName $CategoryName, must be one of (Application,Connectors,CriticalUpdates,DefinitionUpdates,DeveloperKits,FeaturePacks,Guidance,SecurityUpdates,ServicePacks,Tools,UpdateRollups,Updates)" }
        }

        return $CategoryGUID
    }

    Function DoWindowsUpdate
    {
        Param(
        [string]$CategoryName,
        [bool]$IsCheckMode
        )
        
        $CategoryGUID = MapCategoryNameToGUID $CategoryName

        $update_status = @{ changed = $false }

        Write-DebugLog "Creating Windows Update session..."
        $session = New-Object -ComObject Microsoft.Update.Session

        Write-DebugLog "Create Windows Update searcher..."
        $searcher = $session.CreateUpdateSearcher()

        Write-DebugLog "Searching for updates to install in category IDs $CategoryGUID..."
        $searchresult = $searcher.Search("IsInstalled = 0 and CategoryIDs contains '$CategoryGUID'")

        Write-DebugLog "Creating update collection..."
    
        $updates_to_install = New-Object -ComObject Microsoft.Update.UpdateColl

        Write-DebugLog "Found $($searchresult.Updates.Count) updates"

        $update_status.updates = @{ }

        # TODO: add further filtering options
        foreach($update in $searchresult.Updates)
        {
          if(-Not $update.EulaAccepted) 
          {
            Write-DebugLog "Accepting EULA for $($update.Identity.UpdateID)"
            $update.AcceptEula()
          }

          Write-DebugLog "Adding update $($update.Identity.UpdateID) - $($update.Title)"
          $res = $updates_to_install.Add($update)

          $update_status.updates[$update.Identity.UpdateID] = @{
            title = $update.Title
            # TODO: this assumes each update has exactly one KB ID
            kb = $update.KBArticleIDs
            id = $update.Identity.UpdateID
            installed = $false
          }
        }

        # calculate this early for check mode, and to see if we should allow updates to continue
        $sysinfo = New-Object -ComObject Microsoft.Update.SystemInfo
        $update_status.reboot_required = $sysinfo.RebootRequired

        # bail out here for check mode  
        if($IsCheckMode -eq $true) 
        { 
          if($updates_to_install.Count -gt 0) { $update_status.changed = $true }
          return $update_status 
        }

        if($updates_to_install.Count -gt 0) 
        {   
          if($update_status.reboot_required) { throw "A reboot is required before more updates can be installed."}
          Write-DebugLog "Downloading updates..." 
        }

        foreach($update in $updates_to_install)
        {
            if($update.IsDownloaded)
            { 
                Write-DebugLog "Update $($update.Identity.UpdateID) already downloaded, skipping..."
                continue 
            }
            Write-DebugLog "Creating downloader object..."
            $dl = $session.CreateUpdateDownloader()
            Write-DebugLog "Creating download collection..."
            $dl.Updates = New-Object -ComObject Microsoft.Update.UpdateColl
            Write-DebugLog "Adding update $($update.Identity.UpdateID)"
            $res = $dl.Updates.Add($update)
            Write-DebugLog "Downloading update $($update.Identity.UpdateID)..."
            $download_result = $dl.Download()
            # TODO: use OperationResultCode enum instead of int literals
            # TODO: try/catch for better failure messaging (don't just throw an HRESULT)
            if($download_result.ResultCode -ne 2) 
            {
                throw "Failed to download update $($update.Identity.UpdateID)"
            }
        }

        if($updates_to_install.Count -gt 0) { Write-DebugLog "Installing updates..." }

        foreach($update in $updates_to_install)
        {
            Write-DebugLog "Creating installer object..."
            $inst = $session.CreateUpdateInstaller()
            Write-DebugLog "Creating install collection..."
            $inst.Updates = New-Object -ComObject Microsoft.Update.UpdateColl
            Write-DebugLog "Adding update $($update.Identity.UpdateID)"
            $res = $inst.Updates.Add($update)
            Write-DebugLog "Installing update $($update.Identity.UpdateID)..."
            $install_result = $inst.Install()
            # TODO: use OperationResultCode enum instead of int literals
            # TODO: try/catch for better failure messaging (don't just throw an HRESULT)
            if($install_result.ResultCode -ne 2) 
            {
                throw "Failed to install update $($update.Identity.UpdateID) - status was $install_result"
            }
            else { $update_status.changed = $true }

            $update_status.updates[$update.Identity.UpdateID].installed = $true
        }

        # recalculate reboot status after installs
        $sysinfo = New-Object -ComObject Microsoft.Update.SystemInfo
        $update_status.reboot_required = $sysinfo.RebootRequired

        Write-DebugLog $($update_status | out-string)

        Write-DebugLog "Done"

        return $update_status
    }

    Try
    {
        DoWindowsUpdate @boundparms @unboundargs
    }
    Catch
    {
        return @{failed=$true;error=$_.Exception.Message;location=$_.ScriptStackTrace}
    }
}

Function RunAsScheduledJob {
  Param([scriptblock] $job_body, [string] $jobname, [scriptblock] $job_init, [Object[]] $job_arg_list=@())
  
  # try to get a schduled job with the same name (should normally fail)
  $schedjob = Get-ScheduledJob -Name $jobname -ErrorAction SilentlyContinue

  # nuke it if it's there
  # TODO: this can fail if the job is still running (maybe a good thing, maybe not)
  # consider using generated job names + cleanup of dead/finished ones?
  If ($schedjob -ne $null) {
      Unregister-ScheduledJob -Name $jobname
  }

  $schedjob = Register-ScheduledJob -ScriptBlock $job_body -Name $jobname -ArgumentList $job_arg_list -ErrorAction Stop

  # TODO: RunAsTask isn't available in PS3.0- consider a fallback code path using a schedule 2s in the future?
  $schedjob.RunAsTask()

  $sw = [System.Diagnostics.Stopwatch]::StartNew()

  $job = $null

  while ($job -eq $null)
  {
      start-sleep -Milliseconds 100
      if($sw.ElapsedMilliseconds -ge 5000)
      {
        Throw "Timed out waiting for download task to start"
      }
      $job = Wait-Job -Name $schedjob.Name -ErrorAction SilentlyContinue 
  }
 
  # receive-job often returns null even when we got valid output; ignore its output and use $job.Output instead, which is much more reliable
  $jobout_discard = Receive-Job -Job $job -Keep

  # try to nuke the task entry
  Unregister-ScheduledJob -Name $jobname -ErrorAction SilentlyContinue

  Write-Debug $($job.Output | out-string)

  $ret = @{}

  $ret.ErrorOutput = $job.Error
  $ret.WarningOutput = $job.Warning
  $ret.Output = $job.Output
  # TODO: filter extra system-added junk from the output dict (PSComputerName, PSShowComputerName, RunspaceId)
  $ret.VerboseOutput = $job.Verbose
  $ret.DebugOutput = $job.Debug

  return $ret

}

if($args -contains "-Interactive")
{

}
else {
  $parsed_args = Parse-Args $args
  # grr, why use PSCustomObject for args instead of just native hashtable?
  $parsed_args.psobject.properties | foreach -begin {$job_args=@{}} -process {$job_args."$($_.Name)" = $_.Value} -end {$job_args}

  # make booleans actual booleans
  $job_args['IsCheckMode'] = [System.Convert]::ToBoolean($job_args['IsCheckMode'])
}

$sjo = RunAsScheduledJob -job_body $job_body -jobname ansible-win-updates -job_arg_list $job_args

Exit-Json $sjo.Output