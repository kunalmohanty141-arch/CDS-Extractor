// R-25 — an SAP view released C1. Structurally fine, but it must not be
// modified: the modification breaks on upgrade. The tool's answer is a
// Z-wrapper, not an in-place annotation.
@EndUserText.label: 'Vendor'
define view entity I_VENDOR_RELEASED
  as select from lfa1
{
  key lifnr as Supplier,
      name1 as SupplierName,
      land1 as Country
}
