// Sandbox group for sandbox-mux. Resource shapes follow the Container Apps Sandboxes
// Bicep quickstart (API version 2026-02-01-preview). The preview API can change.

@description('Region for all resources. Pick a region where Container Apps Sandboxes is available.')
param location string = resourceGroup().location

@description('Sandbox group name.')
param sandboxGroupName string = 'sandbox-mux'

@description('Upper bound on sandboxes in the group. Keep it at or above pool.max_sandboxes plus triggers.pool.settings.max_sandboxes in config.yaml.')
param maxSandboxCount int = 10

@description('Object ID of the identity the session manager runs as. It receives the data-plane role.')
param orchestratorPrincipalId string

@allowed(['ServicePrincipal', 'User', 'Group'])
param orchestratorPrincipalType string = 'ServicePrincipal'

@description('Set true to attach the group to a new VNet. The subnet cannot be changed afterwards.')
param useVnet bool = false

param vnetAddressPrefix string = '10.0.0.0/16'
param subnetAddressPrefix string = '10.0.0.0/23'

@description('Create a storage account for user files (sandbox-mux files.enabled).')
param enableFiles bool = false

@description('Globally unique storage account name (3-24 lowercase letters and digits).')
param storageAccountName string = 'smux${uniqueString(resourceGroup().id)}'

param filesContainerName string = 'smux-files'

@description('Blobs are deleted this many days after the last write, as a backstop to the service\'s own purge.')
param filesRetentionDays int = 8

// Container Apps SandboxGroup Data Owner
var dataOwnerRoleId = 'c24cf47c-5077-412d-a19c-45202126392c'

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = if (useVnet) {
  name: '${sandboxGroupName}-vnet'
  location: location
  properties: {
    addressSpace: { addressPrefixes: [vnetAddressPrefix] }
    subnets: [
      {
        name: 'sandbox-subnet'
        properties: {
          addressPrefix: subnetAddressPrefix
          delegations: [
            {
              name: 'sandboxDelegation'
              properties: { serviceName: 'Microsoft.App/environments' }
            }
          ]
        }
      }
    ]
  }
}

resource sandboxGroup 'Microsoft.App/sandboxGroups@2026-02-01-preview' = {
  name: sandboxGroupName
  location: location
  properties: {
    maxSandboxCount: maxSandboxCount
  }
}

resource vnetConnection 'Microsoft.App/sandboxGroups/vnetConnections@2026-02-01-preview' = if (useVnet) {
  parent: sandboxGroup
  name: 'default'
  location: location
  properties: {
    subnetId: vnet.properties.subnets[0].id
  }
}

resource dataOwner 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(sandboxGroup.id, orchestratorPrincipalId, dataOwnerRoleId)
  scope: sandboxGroup
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', dataOwnerRoleId)
    principalId: orchestratorPrincipalId
    principalType: orchestratorPrincipalType
  }
}

// ---- user files (optional) ------------------------------------------------------------------

// Storage Blob Data Contributor
var blobContributorRoleId = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = if (enableFiles) {
  name: storageAccountName
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false      // managed identity only
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = if (enableFiles) {
  parent: storage
  name: 'default'
}

resource filesContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = if (enableFiles) {
  parent: blobService
  name: filesContainerName
  properties: { publicAccess: 'None' }
}

resource filesLifecycle 'Microsoft.Storage/storageAccounts/managementPolicies@2023-05-01' = if (enableFiles) {
  parent: storage
  name: 'default'
  properties: {
    policy: {
      rules: [
        {
          name: 'expire-user-files'
          enabled: true
          type: 'Lifecycle'
          definition: {
            filters: { blobTypes: ['blockBlob'] }
            actions: { baseBlob: { delete: { daysAfterModificationGreaterThan: filesRetentionDays } } }
          }
        }
      ]
    }
  }
}

resource filesAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (enableFiles) {
  name: guid(storage.id, orchestratorPrincipalId, blobContributorRoleId)
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', blobContributorRoleId)
    principalId: orchestratorPrincipalId
    principalType: orchestratorPrincipalType
  }
}

output sandboxGroupName string = sandboxGroup.name
output filesAccountUrl string = enableFiles ? 'https://${storageAccountName}.blob.${environment().suffixes.storage}' : ''
output sandboxGroupId string = sandboxGroup.id
