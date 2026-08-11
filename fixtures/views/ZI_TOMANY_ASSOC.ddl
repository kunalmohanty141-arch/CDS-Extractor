// R-12 — a to-many association followed in the projection. Declaring one is
// harmless; following it fans rows out exactly like a to-many join.
@Analytics: { dataExtraction: { enabled: true,
    delta.changeDataCapture.automatic: true } }
define view entity ZI_TOMANY_ASSOC
  as select from zcustorder as Header
  association [0..*] to zorderitem as _Item on _Item.orderid = Header.orderid
{
  key Header.orderid   as OrderId,
      Header.customer  as Customer,
      _Item.material   as Material
}
