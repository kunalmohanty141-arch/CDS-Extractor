// R-11 — an inner join whose joined table is NOT in the CDC mapping.
//
// The counterpart to ZI_INNER_JOIN. VBAK is joined but never mapped, so it
// carries no trigger: change a header and the item rows silently keep their old
// header values, and no delta is raised. That is a real risk, and it is also
// what SAP does deliberately for customizing tables that never change in
// production — so this is MANUAL_REVIEW, not a hard failure.
@Analytics: { dataExtraction: { enabled: true,
    delta.changeDataCapture: {
      mapping: [
        {table: 'VBAP', role: #MAIN, viewElement: ['SalesOrder', 'Item'], tableElement: ['VBELN', 'POSNR']}
      ] } } }
define view entity ZI_INNER_JOIN_UNMAPPED
  as select from vbap as Item
    inner join vbak as Header on Header.vbeln = Item.vbeln
{
  key Item.vbeln    as SalesOrder,
  key Item.posnr    as Item,
      Header.vbeln  as HeaderSalesOrder,
      Header.erdat  as CreatedOn
}
