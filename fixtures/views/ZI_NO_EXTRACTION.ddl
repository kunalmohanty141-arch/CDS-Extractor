// R-01 — structurally fine, but not extraction-enabled. Invisible to Datasphere.
@EndUserText.label: 'Vendor list, not extraction enabled'
define view entity ZI_NO_EXTRACTION
  as select from lfa1
{
  key lifnr as Supplier,
      name1 as SupplierName,
      land1 as Country
}
