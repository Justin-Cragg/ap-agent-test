# Limit the AP agent's Entra app (Mail.ReadWrite / Mail.Send application
# permissions) to the AP mailbox(es) only. Run in Exchange Online PowerShell.
#
#   ./restrict-outlook-app.ps1 -AppId <client-id> -Mailboxes ap@example.com,ap-uk@example.com
param(
  [Parameter(Mandatory)] [string] $AppId,
  [Parameter(Mandatory)] [string[]] $Mailboxes,
  [string] $GroupName = "ap-agent-mailboxes"
)
Connect-ExchangeOnline
$group = Get-DistributionGroup -Identity $GroupName -ErrorAction SilentlyContinue
if (-not $group) {
  $group = New-DistributionGroup -Name $GroupName -Type Security -Members $Mailboxes
} else {
  foreach ($m in $Mailboxes) { Add-DistributionGroupMember -Identity $GroupName -Member $m -ErrorAction SilentlyContinue }
}
New-ApplicationAccessPolicy -AppId $AppId -PolicyScopeGroupId $group.PrimarySmtpAddress -AccessRight RestrictAccess `
  -Description "AP agent may only access AP mailboxes"
# Verify: should be Granted for an AP mailbox and Denied for anyone else
Test-ApplicationAccessPolicy -AppId $AppId -Identity $Mailboxes[0]
