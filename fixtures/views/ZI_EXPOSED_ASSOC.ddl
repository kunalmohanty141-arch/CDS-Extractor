// R-12 negative control, taken from a real finding against SAP's I_BusinessArea.
//
// _Item is to-many and is EXPOSED as an element, but never followed in a path
// expression. That publishes a navigation for consumers; it reads no data and
// multiplies no rows, and ODP extraction ignores it entirely.
//
// Treating this as a violation hard-failed almost every SAP standard view,
// including the specification's own canonical example of working automatic CDC.
@EndUserText.label: 'Order header exposing a to-many navigation'
@Analytics: { dataCategory: #FACT,
  dataExtraction: { enabled: true,
    delta.changeDataCapture.automatic: true } }
define view entity ZI_EXPOSED_ASSOC
  as select from zcustorder as Header
  association [0..*] to zorderitem as _Item on _Item.orderid = Header.orderid
{
  key Header.orderid   as OrderId,
      Header.customer  as Customer,
      Header.amount    as Amount,
      _Item                                  -- exposed, never followed
}
